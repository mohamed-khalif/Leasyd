"""Index lookup: which Parquet files could hold the rows a query asks for.

    {"tenant": "acme",                      # required
     "signal": "logs",                      # default logs
     "services": ["checkout"],              # optional; default every service
     "start": "2026-09-26T10:30:00Z",       # inclusive
     "end":   "2026-09-26T11:15:00Z",       # inclusive
     "match": {"trace_id": "4bf9..."}}      # optional; ANDed, bloom-checked

Returns the files with their time range, row count and size, plus how many
candidates each stage kept, so callers (and tests) can see the pruning.

Every read goes through obs-tenant-reader, assumed with the tenant as a
session tag. IAM then refuses any index key or object outside that tenant,
so even a bug here can't return another tenant's files. (In the query API
the tenant comes from the caller's credentials, never from the request.)

The time range needs no S3 listing: each file spans at most one hour (the
compactor guarantees it), so every file overlapping [start, end] has
min_ts in [start - 1h, end]. Query that sort-key range, then drop files
ending before start.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import boto3

import bloom
import layout

TABLE = os.environ["INDEX_TABLE"]
BUCKET = os.environ["BUCKET"]
TENANT_READER_ROLE = os.environ["TENANT_READER_ROLE_ARN"]
MAX_FILE_SPAN = timedelta(hours=1)
SESSION_SECONDS = 3600
REFRESH_BEFORE_EXPIRY = 300

sts = boto3.client("sts")
_sessions = {}  # tenant -> (boto3.Session, expiry epoch); reused across warm invocations


def handler(event, context):
    return lookup(**event)


def lookup(tenant, start, end, signal="logs", services=None, match=None):
    t0 = time.perf_counter()
    layout.check_tenant(tenant)
    start_dt, end_dt = _parse(start), _parse(end)
    if end_dt < start_dt:
        raise ValueError("end is before start")
    terms = [bloom.term(f, v) for f, v in sorted((match or {}).items())]
    ddb, s3 = _clients_for(tenant)
    services = services or _all_services(ddb, tenant, signal)

    lo = _iso(start_dt - MAX_FILE_SPAN)
    hi = _iso(end_dt) + "#￿"  # every sort key with min_ts <= end
    stats = {"services": len(services), "in_time_range": 0, "after_bloom": 0, "read_units": 0.0}
    files = []
    for service in services:
        for item in _query(ddb, layout.index_pk(tenant, signal, service), lo, hi, _iso(start_dt), stats):
            stats["in_time_range"] += 1
            if terms and not _bloom_says_maybe(s3, item, terms):
                continue
            stats["after_bloom"] += 1
            files.append({
                "service": service,
                "file_path": item["file_path"]["S"],
                "min_ts": item["min_ts"]["S"],
                "max_ts": item["max_ts"]["S"],
                "row_count": int(item["row_count"]["N"]),
                "size_bytes": int(item["size_bytes"]["N"]),
                "storage_class": item.get("storage_class", {}).get("S", "STANDARD"),
            })
    files.sort(key=lambda f: (f["min_ts"], f["file_path"]))
    stats["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    result = {"files": files, "stats": stats}
    print(json.dumps({"lookup": {"tenant": tenant, "start": start, "end": end, "services": services,
                                 "match": sorted(match or {})},
                      "stats": stats}))
    return result


def _clients_for(tenant):
    """DynamoDB and S3 clients whose credentials only reach this tenant."""
    session, expiry = _sessions.get(tenant, (None, 0))
    if time.time() > expiry - REFRESH_BEFORE_EXPIRY:
        creds = sts.assume_role(
            RoleArn=TENANT_READER_ROLE, RoleSessionName=f"lookup-{tenant}"[:64],
            DurationSeconds=SESSION_SECONDS, Tags=[{"Key": "tenant", "Value": tenant}],
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        expiry = creds["Expiration"].timestamp()
        _sessions[tenant] = (session, expiry)
    return session.client("dynamodb"), session.client("s3")


def _query(ddb, pk, lo, hi, start_iso, stats):
    kwargs = dict(
        TableName=TABLE,
        KeyConditionExpression="pk = :pk AND sk BETWEEN :lo AND :hi",
        FilterExpression="max_ts >= :start",
        ExpressionAttributeValues={":pk": {"S": pk}, ":lo": {"S": lo}, ":hi": {"S": hi},
                                   ":start": {"S": start_iso}},
        ReturnConsumedCapacity="TOTAL",
    )
    while True:
        page = ddb.query(**kwargs)
        stats["read_units"] += page.get("ConsumedCapacity", {}).get("CapacityUnits", 0)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _bloom_says_maybe(s3, item, terms):
    """False only if the file certainly holds none of the terms. Files
    indexed before blooms existed, or indexing other fields, always pass."""
    fields = {f["S"] for f in item.get("bloom_fields", {}).get("L", [])}
    checkable = [t for t in terms if t.split("=", 1)[0] in fields]
    if not checkable:
        return True
    if "bloom" in item:
        bits = item["bloom"]["B"]
    elif "bloom_s3_key" in item:
        bits = s3.get_object(Bucket=BUCKET, Key=item["bloom_s3_key"]["S"])["Body"].read()
    else:
        return True
    b = bloom.Bloom(int(item["bloom_m"]["N"]), int(item["bloom_k"]["N"]), bits)
    return all(b.might_contain(t) for t in checkable)


def _all_services(ddb, tenant, signal):
    resp = ddb.query(
        TableName=TABLE, KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": layout.services_pk(tenant, signal)}},
    )
    return sorted(s for item in resp["Items"] for s in item.get("services", {}).get("SS", []))


def _parse(ts):
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(dt):
    """Same format the compactor writes for min_ts / max_ts."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
