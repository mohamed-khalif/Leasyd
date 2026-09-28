"""Index lookup: which files could hold the rows a query asks for.

    {"tenant": "acme",                      # required
     "signal": "logs",                      # default logs
     "services": ["checkout"],              # optional; default every service
     "start": "2026-09-26T10:30:00Z",       # inclusive
     "end":   "2026-09-26T11:15:00Z",       # inclusive
     "match": {"trace_id": "4bf9..."}}      # optional; ANDed, bloom-checked

Returns the files with their kind ("parquet", compacted; or "raw", the fast
lane's Parquet copy of one raw file not yet compacted), time range, row count and size,
plus how many candidates each stage kept, so callers can see the pruning.

Fast lane handover: each raw file belongs to a compaction plan for its
arrival hour, and that plan's Parquet output replaces it. The plan's status
decides which copy is visible, so a query never counts a record twice or
misses it:
  plan absent or "planned" (being compacted)  -> raw visible, Parquet hidden
  plan "committed"                            -> Parquet visible, raw hidden
  plan gone after cleanup                     -> only Parquet remains

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config

import bloom
import dayfilter
import layout

TABLE = os.environ["INDEX_TABLE"]
BUCKET = os.environ["BUCKET"]
TENANT_READER_ROLE = os.environ["TENANT_READER_ROLE_ARN"]
MAX_FILE_SPAN = timedelta(hours=1)
# Parquet entries older than this are from long-finished plans; skip checking
# their plan (a plan stuck longer than this also trips the stuck alarm).
PLAN_CHECK_WINDOW = timedelta(hours=6)
QUERY_THREADS = 16   # index partitions (services) queried at once
BLOOM_THREADS = 64   # bloom files fetched from S3 at once
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
    stats = {"services": len(services), "in_time_range": 0, "after_bloom": 0, "read_units": 0.0,
             "hidden_by_handover": 0, "bloom_fetches": 0}

    # Services are separate index partitions: query them in parallel.
    def per_service(service):
        st = {"read_units": 0.0}
        return [(service, it) for it in _query(ddb, layout.index_pk(tenant, signal, service), lo, hi,
                                                _iso(start_dt), st)], st["read_units"]
    in_range = []
    with ThreadPoolExecutor(min(QUERY_THREADS, max(1, len(services)))) as pool:
        for found, ru in pool.map(per_service, services):
            in_range += found
            stats["read_units"] += ru
    stats["in_time_range"] = len(in_range)

    candidates = in_range
    if terms:
        # Whole days first: a sealed day filter that rules the IDs out drops
        # every Parquet file of that day without reading their blooms.
        candidates = _day_filter(ddb, s3, tenant, signal, candidates, terms, stats)
        in_range = candidates
        # Blooms too big for the index item are in S3: fetch each one once, in
        # parallel (a fast-lane raw file's entries, one per service, share one).
        shared = {it["bloom_s3_key"]["S"]: it for _, it in in_range if "bloom_s3_key" in it}
        with ThreadPoolExecutor(BLOOM_THREADS) as pool:
            fetched = dict(zip(shared, pool.map(lambda it: _bloom_says_maybe(s3, it, terms), shared.values())))
        stats["bloom_fetches"] = sum(1 for it in shared.values() if _checkable(it, terms))
        keep = [fetched[it["bloom_s3_key"]["S"]] if "bloom_s3_key" in it else _bloom_says_maybe(s3, it, terms)
                for _, it in in_range]
        candidates = [c for c, k in zip(in_range, keep) if k]
    stats["after_bloom"] = len(candidates)

    visible = _visible(ddb, candidates, stats)
    files = []
    for service, item in visible:
        files.append({
            "service": service,
            "kind": item.get("kind", {}).get("S", "parquet"),
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


def _visible(ddb, candidates, stats):
    """Apply the fast-lane handover rule (see module docstring)."""
    recent = _iso(datetime.now(timezone.utc) - PLAN_CHECK_WINDOW)
    need = set()
    for _, item in candidates:
        kind = item.get("kind", {}).get("S", "parquet")
        if "plan_pk" in item and (kind == "raw" or item.get("compacted_at", {}).get("S", "") >= recent):
            need.add(item["plan_pk"]["S"])
    status = {}           # (plan_pk, batch_id) -> "planned" | "committed"
    committed_inputs = {}  # plan_pk -> raw keys in committed plans
    for pk in need:
        for plan in _plans(ddb, pk, stats):
            status[(pk, plan["sk"]["S"])] = plan["status"]["S"]
            if plan["status"]["S"] == "committed":
                committed_inputs.setdefault(pk, set()).update(v["S"] for v in plan["inputs"]["L"])

    out = []
    for service, item in candidates:
        kind = item.get("kind", {}).get("S", "parquet")
        pk = item.get("plan_pk", {}).get("S")
        if kind == "parquet" and status.get((pk, item.get("batch_id", {}).get("S"))) == "planned":
            stats["hidden_by_handover"] += 1   # its plan isn't committed yet: the raw copy is visible
            continue
        if kind == "raw" and item["raw_key"]["S"] in committed_inputs.get(pk, ()):
            stats["hidden_by_handover"] += 1   # already replaced by committed Parquet
            continue
        out.append((service, item))
    return out


def _plans(ddb, pk, stats):
    kwargs = dict(TableName=TABLE, KeyConditionExpression="pk = :pk", ConsistentRead=True,
                  ProjectionExpression="sk, #s, inputs", ExpressionAttributeNames={"#s": "status"},
                  ExpressionAttributeValues={":pk": {"S": pk}}, ReturnConsumedCapacity="TOTAL")
    while True:
        page = ddb.query(**kwargs)
        stats["read_units"] += page.get("ConsumedCapacity", {}).get("CapacityUnits", 0)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


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
    # Enough connections for the parallel queries and bloom fetches (boto3's default is 10).
    cfg = botocore.config.Config(max_pool_connections=BLOOM_THREADS + QUERY_THREADS)
    return session.client("dynamodb", config=cfg), session.client("s3", config=cfg)


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


def _day_filter(ddb, s3, tenant, signal, candidates, terms, stats):
    """Drop Parquet candidates in days, then hours, whose sealed filter rules
    the IDs out. Days use day filters; days without a trusted one (today, or
    changed since sealing) use hour filters for their hours."""
    stats.update(days_checked=0, days_ruled_out=0, hours_checked=0, hours_ruled_out=0)
    parquet = [it for _, it in candidates if it.get("kind", {}).get("S", "parquet") == "parquet"]
    days = sorted({it["min_ts"]["S"][:10] for it in parquet})
    if not days:
        return candidates
    fields = {t.split("=", 1)[0] for t in terms}

    checked, out_days = _check_periods(
        ddb, s3, layout.day_pk(tenant, signal), days, terms, fields, stats,
        lambda day, v, g: layout.day_filter_key(tenant, signal, day, v, g))
    stats["days_checked"], stats["days_ruled_out"] = len(checked), len(out_days)

    hours = sorted({it["min_ts"]["S"][:13] for it in parquet if it["min_ts"]["S"][:10] not in checked})
    h_checked, out_hours = _check_periods(
        ddb, s3, layout.hour_pk(tenant, signal), hours, terms, fields, stats,
        lambda dh, v, g: layout.hour_filter_key(tenant, signal, dh[:10], dh[11:13], v, g))
    stats["hours_checked"], stats["hours_ruled_out"] = len(h_checked), len(out_hours)

    def ruled_out(it):
        if it.get("kind", {}).get("S", "parquet") != "parquet":
            return False  # raw files are never in a sealed filter
        return it["min_ts"]["S"][:10] in out_days or it["min_ts"]["S"][:13] in out_hours
    return [(svc, it) for svc, it in candidates if not ruled_out(it)]


def _check_periods(ddb, s3, pk, periods, terms, fields, stats, filter_key):
    """(periods with a trusted filter, those whose filter rules the terms out).
    Periods are day ("2026-09-27") or hour ("2026-09-27T22") sort keys."""
    if not periods:
        return set(), set()
    items, kw = [], dict(TableName=TABLE, KeyConditionExpression="pk = :p AND sk BETWEEN :a AND :b",
                         ExpressionAttributeValues={":p": {"S": pk}, ":a": {"S": periods[0]}, ":b": {"S": periods[-1]}},
                         ReturnConsumedCapacity="TOTAL")
    while True:
        resp = ddb.query(**kw)
        stats["read_units"] += resp.get("ConsumedCapacity", {}).get("CapacityUnits", 0)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            break
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # Trusted only while no chunk was added after sealing, and only if every
    # chunk of the period indexed the fields asked about.
    def usable(it):
        fs = it.get("fieldsets", {}).get("SS", [])
        return ("groups" in it and it.get("sealed", {}).get("N") == it.get("dirty", {}).get("N")
                and len(fs) == 1 and fields <= set(fs[0].split(",")))
    wanted = set(periods)
    sealed = {it["sk"]["S"]: it for it in items if it["sk"]["S"] in wanted and usable(it)}

    def maybe(period):
        item = sealed[period]
        for t in terms:
            d = dayfilter.digest(t)
            g = dayfilter.group_of(d)
            geo = item["groups"]["M"][str(g)]["M"]
            subshards, m = int(geo["s"]["N"]), int(geo["m"]["N"])
            lo, hi = dayfilter.byte_range(d, subshards, m)
            try:
                bits = s3.get_object(Bucket=BUCKET, Key=filter_key(period, item["sealed"]["N"], g),
                                     Range=f"bytes={lo}-{hi}")["Body"].read()
            except s3.exceptions.NoSuchKey:
                return True  # re-sealed meanwhile and this version removed: can't rule out
            if not dayfilter.might_contain(bits, d, subshards, m):
                return False
        return True

    check = sorted(sealed)
    with ThreadPoolExecutor(BLOOM_THREADS) as pool:
        out = {p for p, keep in zip(check, pool.map(maybe, check)) if not keep}
    return set(check), out


def _checkable(item, terms):
    fields = {f["S"] for f in item.get("bloom_fields", {}).get("L", [])}
    return [t for t in terms if t.split("=", 1)[0] in fields]


def _bloom_says_maybe(s3, item, terms):
    """False only if the file certainly holds none of the terms. Files
    indexed before blooms existed, or indexing other fields, always pass."""
    checkable = _checkable(item, terms)
    if not checkable:
        return True
    if "bloom" in item:
        bits = item["bloom"]["B"]
    elif "bloom_s3_key" in item:
        try:
            bits = s3.get_object(Bucket=BUCKET, Key=item["bloom_s3_key"]["S"])["Body"].read()
        except s3.exceptions.NoSuchKey:
            # A raw entry whose file was just compacted: its bloom is deleted
            # after the plan commits. Keep it; the handover check hides it.
            return True
    else:
        return True
    m = int(item["bloom_m"]["N"])
    if len(bits) * 8 < m:
        return True  # not this item's bloom (files written before a naming fix): can't rule the file out
    b = bloom.Bloom(m, int(item["bloom_k"]["N"]), bits)
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
