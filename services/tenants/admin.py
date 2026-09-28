"""Tenant operations (T5): the platform's control plane, one Lambda.

Invoke with {"action": ..., ...}; infra/tenant.sh wraps it.

  create  {tenant, plan}         ingest streams, tenant record, first API key (returned once)
  rotate  {tenant, grace_hours}  a new key; the tenant's other keys keep working for
                                 grace_hours (default 24), then are refused
  revoke  {tenant, key_id?}      refuse one key, or all of the tenant's keys
  delete  {tenant}               refuse all keys, remove the streams, then purge every
                                 object and index entry of the tenant (see below)
  status  {tenant}               tenant record and keys (never the keys themselves)
  usage   {tenant, start, end}   records and bytes compacted per day and signal
  list    {}                     every tenant
  sweep   {}                     scheduled: expire rotated keys, advance deletions
  purge   {tenant}               internal: one purge pass (re-invokes itself if long)

Keys: only their SHA-256 is stored (obs-tenants, pk "key#<hash>"); the
authorizer maps a key to its tenant from there. API Gateway also holds each
key, for the tenant's usage plan (rate limits).

Deletion: the tenant is marked "deleting" and its keys revoked, so nothing
new is accepted (API Gateway may honour a cached "allow" for up to a minute).
Its Firehose streams are deleted. A purge pass then deletes everything under
_incoming/tenant=<T>/, _incoming/_errors/tenant=<T>/ and data/tenant=<T>/, and
every obs-index item whose key starts "<T>#" or "_lease#<T>#". A compaction
worker or fast-lane indexer already running for the tenant can still write
after that pass, so passes repeat SETTLE_MINUTES apart (longer than a
worker's lease) until one finds nothing; then the tenant is "deleted".
Usage records are the platform's billing records and are kept.
"""

import decimal
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key

TENANTS = os.environ["TENANTS_TABLE"]
INDEX = os.environ["INDEX_TABLE"]
USAGE = os.environ["USAGE_TABLE"]
BUCKET = os.environ["BUCKET"]
FIREHOSE_ROLE = os.environ["FIREHOSE_ROLE_ARN"]
PLANS = json.loads(os.environ.get("USAGE_PLANS", "{}"))  # plan name -> API Gateway usage plan id
SELF = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "obs-tenant-admin")
BUFFER_SECONDS = int(os.environ.get("BUFFER_SECONDS", "30"))
SETTLE = timedelta(minutes=int(os.environ.get("SETTLE_MINUTES", "20")))
SIGNALS = ("logs", "traces", "metrics")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
_KEY_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

ddb = boto3.resource("dynamodb")
tenants = ddb.Table(TENANTS)
index = ddb.Table(INDEX)
usage_t = ddb.Table(USAGE)
s3 = boto3.client("s3")
apigw = boto3.client("apigateway")
firehose = boto3.client("firehose")
lam = boto3.client("lambda")


class Refused(Exception):
    pass


def handler(event, context):
    action = event.get("action")
    fn = ACTIONS.get(action)
    if fn is None:
        return {"error": f"unknown action {action!r}; one of {sorted(ACTIONS)}"}
    args = {k: v for k, v in event.items() if k != "action"}
    if action not in ("list", "sweep"):
        args["tenant"] = check_tenant(args.get("tenant"))
    try:
        out = fn(context=context, **args)
    except Refused as e:
        out = {"error": str(e)}
    out = json.loads(json.dumps(out, default=_plain))
    # Never log a key.
    print(json.dumps({"action": action, "tenant": args.get("tenant"),
                      "result": {k: v for k, v in out.items() if k != "api_key"}}, default=str))
    return out


def _plain(v):
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, set):
        return sorted(v)
    return str(v)


def check_tenant(tenant):
    if not isinstance(tenant, str) or not _TENANT.match(tenant):
        raise ValueError(f"invalid tenant id {tenant!r}: 3-40 chars of a-z, 0-9, '-'")
    return tenant


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ tenant record

def _record(tenant):
    return tenants.get_item(Key={"pk": f"tenant#{tenant}"}, ConsistentRead=True).get("Item")


def _keys(tenant):
    items, kw = [], dict(IndexName="by-tenant", KeyConditionExpression=Key("tenant").eq(tenant)
                         & Key("pk").begins_with("key#"))
    while True:
        page = tenants.query(**kw)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# ------------------------------------------------------------------ actions

def create(tenant, plan="standard", context=None):
    if plan not in PLANS:
        raise Refused(f"unknown plan {plan!r}; one of {sorted(PLANS)}")
    rec = _record(tenant)
    if rec and rec["status"] == "deleting":
        raise Refused(f"{tenant} is being deleted; wait until it is deleted")
    if rec and rec["status"] == "active":
        raise Refused(f"{tenant} already exists; use rotate for a new key")
    _provision_streams(tenant)
    tenants.put_item(Item={"pk": f"tenant#{tenant}", "tenant": tenant, "status": "active", "plan": plan,
                           "created_at": _iso(_now())})
    key, key_id = _issue_key(tenant, plan)
    return {"tenant": tenant, "plan": plan, "key_id": key_id, "api_key": key}


def rotate(tenant, grace_hours=24, context=None):
    rec = _active(tenant)
    old = [k for k in _keys(tenant) if k["status"] == "active"]
    key, key_id = _issue_key(tenant, rec.get("plan", "standard"))
    expires = _iso(_now() + timedelta(hours=float(grace_hours)))
    for k in old:
        tenants.update_item(Key={"pk": k["pk"]}, UpdateExpression="SET #s = :e, expires_at = :x",
                            ConditionExpression="#s = :a", ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":e": "expiring", ":x": expires, ":a": "active"})
    return {"tenant": tenant, "key_id": key_id, "api_key": key,
            "old_keys_expire_at": expires if old else None, "old_key_ids": [k["api_key_id"] for k in old]}


def revoke(tenant, key_id=None, context=None):
    keys = [k for k in _keys(tenant) if k["status"] in ("active", "expiring")
            and (key_id is None or k["api_key_id"] == key_id)]
    if key_id and not keys:
        raise Refused(f"no live key {key_id} for {tenant}")
    for k in keys:
        _revoke_key(k)
    return {"tenant": tenant, "revoked": [k["api_key_id"] for k in keys]}


def delete(tenant, context=None):
    rec = _record(tenant)
    legacy = rec is None and _keys(tenant)  # created before T5: keys but no tenant record
    if rec is None and not legacy:
        raise Refused(f"no tenant {tenant}")
    if rec and rec["status"] == "deleted":
        return {"tenant": tenant, "status": "deleted"}
    tenants.put_item(Item={**(rec or {"pk": f"tenant#{tenant}", "tenant": tenant, "plan": "standard"}),
                           "status": "deleting", "delete_requested_at": _iso(_now()), "passes": 0,
                           # if the first pass fails, the sweep retries it after this
                           "next_pass_at": _iso(_now() + SETTLE)})
    revoked = [k["api_key_id"] for k in _keys(tenant) if k["status"] in ("active", "expiring")]
    for k in _keys(tenant):
        _revoke_key(k, delete_from_gateway=True)
    for sig in SIGNALS:
        try:
            firehose.delete_delivery_stream(DeliveryStreamName=f"obs-t-{tenant}-{sig}")
        except firehose.exceptions.ResourceNotFoundException:
            pass
    _invoke_self({"action": "purge", "tenant": tenant})
    return {"tenant": tenant, "status": "deleting", "revoked": revoked,
            "note": f"data purge started; passes repeat every {int(SETTLE.total_seconds() // 60)} min "
                    "until one finds nothing"}


def status(tenant, context=None):
    rec = _record(tenant) or {"status": "unknown (no tenant record)"}
    keys = [{k: v for k, v in key.items() if k != "pk"} for key in _keys(tenant)]
    return {"tenant": tenant, **{k: v for k, v in rec.items() if k not in ("pk", "tenant")},
            "keys": sorted(keys, key=lambda k: k.get("created_at", ""))}


def usage(tenant, start=None, end=None, context=None):
    """Per day (of arrival) and signal: records, raw bytes received (gzipped
    OTLP JSON, as Firehose delivered it) and Parquet bytes stored. Counted
    when compaction commits, so the current hour shows up about an hour later."""
    end = end or _now().strftime("%Y-%m-%d")
    start = start or (_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    days, kw = {}, dict(KeyConditionExpression=Key("tenant").eq(tenant) & Key("sk").between(start, end + "#~"))
    while True:
        page = usage_t.query(**kw)
        for it in page["Items"]:
            d = days.setdefault(it["dt"], {}).setdefault(it["signal"], {"records": 0, "raw_bytes": 0,
                                                                       "stored_bytes": 0, "files": 0})
            for f in d:
                d[f] += int(it.get(f, 0))
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    totals = {}
    for sigs in days.values():
        for sig, d in sigs.items():
            t = totals.setdefault(sig, dict.fromkeys(d, 0))
            for f in d:
                t[f] += d[f]
    return {"tenant": tenant, "start": start, "end": end, "days": dict(sorted(days.items())), "totals": totals}


def list_tenants(context=None):
    out, kw = [], dict(FilterExpression=Attr("pk").begins_with("tenant#"))
    while True:
        page = tenants.scan(**kw)
        out += [{"tenant": it["tenant"], "status": it["status"], "plan": it.get("plan"),
                 "created_at": it.get("created_at")} for it in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return {"tenants": sorted(out, key=lambda t: t["tenant"])}
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def sweep(context=None):
    """Scheduled. Refuse rotated keys whose grace has ended; start the next
    purge pass of deletions that are due; finish deletions whose last pass
    (after at least one settle interval) found nothing."""
    now = _iso(_now())
    expired, advanced, finished = [], [], []
    kw = dict(FilterExpression=Attr("status").is_in(["expiring", "deleting"]))
    while True:
        page = tenants.scan(**kw)
        for it in page["Items"]:
            if it["pk"].startswith("key#") and it["status"] == "expiring" and it["expires_at"] <= now:
                _revoke_key(it)
                expired.append(it["api_key_id"])
            elif it["pk"].startswith("tenant#") and it["status"] == "deleting":
                # Passes run SETTLE apart, so a clean pass after the first
                # means nothing was still being written.
                if int(it.get("passes", 0)) >= 2 and int(it.get("last_pass_deleted", 1)) == 0:
                    tenants.update_item(Key={"pk": it["pk"]}, UpdateExpression="SET #s = :d, deleted_at = :n",
                                        ExpressionAttributeNames={"#s": "status"},
                                        ExpressionAttributeValues={":d": "deleted", ":n": now})
                    finished.append(it["tenant"])
                elif it["next_pass_at"] <= now:
                    _invoke_self({"action": "purge", "tenant": it["tenant"]})
                    advanced.append(it["tenant"])
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return {"expired_keys": expired, "purging": advanced, "deleted": finished}


def purge(tenant, deleted=0, context=None):
    """One purge pass. Deletes until nothing is left or the invocation is
    nearly out of time, then re-invokes itself to carry on the same pass."""
    rec = _record(tenant)
    if not rec or rec["status"] != "deleting":
        raise Refused(f"{tenant} is not being deleted")
    deadline = time.time() + (context.get_remaining_time_in_millis() / 1000 - 60 if context else 600)
    done = True
    for prefix in (f"_incoming/tenant={tenant}/", f"_incoming/_errors/tenant={tenant}/", f"data/tenant={tenant}/"):
        n, finished = _purge_prefix(prefix, deadline)
        deleted += n
        done = done and finished
    if done:
        n, done = _purge_index(tenant, deadline)
        deleted += n
    if not done:
        _invoke_self({"action": "purge", "tenant": tenant, "deleted": deleted})
        return {"tenant": tenant, "deleted_so_far": deleted, "continuing": True}
    tenants.update_item(
        Key={"pk": f"tenant#{tenant}"},
        UpdateExpression="SET passes = if_not_exists(passes, :z) + :one, last_pass_deleted = :d, "
                         "last_pass_at = :n, next_pass_at = :next",
        ExpressionAttributeValues={":z": 0, ":one": 1, ":d": deleted, ":n": _iso(_now()),
                                   ":next": _iso(_now() + SETTLE)})
    return {"tenant": tenant, "deleted": deleted, "pass_complete": True}


ACTIONS = {"create": create, "rotate": rotate, "revoke": revoke, "delete": delete, "status": status,
           "usage": usage, "list": list_tenants, "sweep": sweep, "purge": purge}


# ------------------------------------------------------------------ helpers

def _active(tenant):
    rec = _record(tenant)
    if rec is None and _keys(tenant):
        return {"plan": "standard"}  # created before T5
    if not rec or rec["status"] != "active":
        raise Refused(f"{tenant} is not an active tenant")
    return rec


def _issue_key(tenant, plan):
    key = "obs_" + "".join(secrets.choice(_KEY_CHARS) for _ in range(40))
    key_id = apigw.create_api_key(name=f"{tenant}-{_now():%Y%m%dT%H%M%S}-{secrets.token_hex(2)}", value=key,
                                  enabled=True, tags={"tenant": tenant, "project": "obs"})["id"]
    apigw.create_usage_plan_key(usagePlanId=PLANS[plan], keyId=key_id, keyType="API_KEY")
    tenants.put_item(Item={"pk": f"key#{key_hash(key)}", "tenant": tenant, "status": "active",
                           "api_key_id": key_id, "plan": plan, "created_at": _iso(_now())},
                     ConditionExpression="attribute_not_exists(pk)")
    return key, key_id


def _revoke_key(k, delete_from_gateway=False):
    # The status change makes the authorizer refuse the key once API Gateway's
    # cached answer expires (up to a minute); disabling it in API Gateway
    # usually refuses it sooner.
    if k["status"] != "revoked":
        tenants.update_item(Key={"pk": k["pk"]}, UpdateExpression="SET #s = :r, revoked_at = :n",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":r": "revoked", ":n": _iso(_now())})
    try:
        if delete_from_gateway:
            apigw.delete_api_key(apiKey=k["api_key_id"])
        elif k["status"] != "revoked":
            apigw.update_api_key(apiKey=k["api_key_id"],
                                 patchOperations=[{"op": "replace", "path": "/enabled", "value": "false"}])
    except apigw.exceptions.NotFoundException:
        pass


def _provision_streams(tenant):
    """One Firehose stream per signal, obs-t-<tenant>-<signal>, delivering
    gzipped OTLP JSON to _incoming/tenant=<T>/<signal>/. Returns once all are
    ACTIVE (about a minute for new streams)."""
    for sig in SIGNALS:
        name = f"obs-t-{tenant}-{sig}"
        try:
            firehose.create_delivery_stream(
                DeliveryStreamName=name, DeliveryStreamType="DirectPut",
                ExtendedS3DestinationConfiguration={
                    "RoleARN": FIREHOSE_ROLE, "BucketARN": f"arn:aws:s3:::{BUCKET}",
                    "Prefix": f"_incoming/tenant={tenant}/{sig}/dt=!{{timestamp:yyyy-MM-dd}}/hour=!{{timestamp:HH}}/",
                    "ErrorOutputPrefix": f"_incoming/_errors/tenant={tenant}/{sig}/!{{firehose:error-output-type}}"
                                         "/dt=!{timestamp:yyyy-MM-dd}/",
                    "BufferingHints": {"SizeInMBs": 64, "IntervalInSeconds": BUFFER_SECONDS},
                    # Ingest gzips each record before sending (Firehose bills received
                    # bytes), so the stream passes them through: its objects are gzip
                    # members back to back, a valid .gz file.
                    "CompressionFormat": "UNCOMPRESSED", "FileExtension": ".json.gz",
                },
                Tags=[{"Key": "tenant", "Value": tenant}, {"Key": "project", "Value": "obs"}])
        except firehose.exceptions.ResourceInUseException:
            pass  # already exists
    for sig in SIGNALS:
        for _ in range(60):
            st = firehose.describe_delivery_stream(DeliveryStreamName=f"obs-t-{tenant}-{sig}")
            if st["DeliveryStreamDescription"]["DeliveryStreamStatus"] == "ACTIVE":
                break
            time.sleep(5)
        else:
            raise Refused(f"stream obs-t-{tenant}-{sig} not active after 5 minutes")


def _purge_prefix(prefix, deadline):
    deleted = 0
    while time.time() < deadline:
        page = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=1000)
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if not keys:
            return deleted, True
        resp = s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys, "Quiet": True})
        if resp.get("Errors"):
            raise RuntimeError(f"delete failed: {resp['Errors'][:3]}")
        deleted += len(keys)
    return deleted, False


def _purge_index(tenant, deadline):
    """Every obs-index item of the tenant. A scan, since the tenant's keys
    span many partitions (per service, plan hour, raw hour); deletions are
    rare and a scan costs cents even for a large index."""
    deleted = 0
    cond = Attr("pk").begins_with(f"{tenant}#") | Attr("pk").begins_with(f"_lease#{tenant}#")
    kw = dict(FilterExpression=cond, ProjectionExpression="pk, sk")
    while True:
        page = index.scan(**kw)
        with index.batch_writer() as w:
            for it in page["Items"]:
                w.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
        deleted += len(page["Items"])
        if "LastEvaluatedKey" not in page:
            return deleted, True
        if time.time() >= deadline:
            return deleted, False  # the next invocation rescans from the start
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _invoke_self(payload):
    lam.invoke(FunctionName=SELF, InvocationType="Event", Payload=json.dumps(payload).encode())
