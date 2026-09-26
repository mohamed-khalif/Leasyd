"""Compaction Lambdas.

dispatcher: on a schedule, finds closed hours under _incoming/ and invokes
            one worker per (signal, arrival hour).
worker:     compacts one hour's raw files into Parquet, indexes them, then
            deletes the raw files.

Worker steps, in this order, so any crash can be re-run safely:
  0. take a lease on the partition, so only one worker runs per hour
  1. finish any batch a previous run committed but didn't finish deleting
  2. write Parquet   part-<batch_id>.parquet   (same inputs -> same keys)
  3. write index     sk = <min_ts>#<batch_id>  (same inputs -> same keys)
  4. commit          manifest item listing the input keys
  5. delete inputs
  6. delete manifest
A crash before 4 re-runs on the same inputs and overwrites. A crash after 4
is finished by step 1 on the next run, so no input is ever compacted twice.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone

import boto3

import compact

BUCKET = os.environ["BUCKET"]
TABLE = os.environ["INDEX_TABLE"]
WORKER = os.environ.get("WORKER_FUNCTION", "")
SIGNALS = [s for s in os.environ.get("COMPACT_SIGNALS", "logs").split(",") if s]
GRACE = timedelta(minutes=int(os.environ.get("GRACE_MINUTES", "10")))
MAX_INPUT_FILES = int(os.environ.get("MAX_INPUT_FILES", "2000"))
MAX_INPUT_BYTES = int(os.environ.get("MAX_INPUT_BYTES", str(200 * 1024 * 1024)))
ALLOW_CRASH_INJECTION = os.environ.get("ALLOW_CRASH_INJECTION") == "true"
LEASE_SECONDS = 16 * 60  # longer than the worker's 15-minute maximum timeout

s3 = boto3.client("s3")
ddb = boto3.client("dynamodb")
lam = boto3.client("lambda")
cw = boto3.client("cloudwatch")


# ---------------------------------------------------------------- dispatcher

def dispatcher(event, context):
    now = datetime.now(timezone.utc)
    invoked = []
    for signal in SIGNALS:
        partitions = list(_incoming_partitions(signal))
        oldest = min((start for _, _, start in partitions), default=None)
        age = (now - oldest).total_seconds() / 60 if oldest else 0
        cw.put_metric_data(
            Namespace="obs",
            MetricData=[{
                "MetricName": "OldestIncomingAgeMinutes",
                "Dimensions": [{"Name": "signal", "Value": signal}],
                "Value": age, "Unit": "None",
            }],
        )
        for dt, hour, start in partitions:
            if now >= start + timedelta(hours=1) + GRACE:
                _invoke_worker({"signal": signal, "dt": dt, "hour": hour})
                invoked.append(f"{signal}/{dt}/{hour}")
    print(json.dumps({"invoked": invoked}))
    return {"invoked": invoked}


def _incoming_partitions(signal):
    for dt_prefix in _common_prefixes(f"_incoming/{signal}/"):
        dt = dt_prefix.rstrip("/").rsplit("dt=", 1)[1]
        for hour_prefix in _common_prefixes(dt_prefix):
            hour = hour_prefix.rstrip("/").rsplit("hour=", 1)[1]
            start = datetime.strptime(f"{dt} {hour}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc)
            yield dt, hour, start


def _common_prefixes(prefix):
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            yield p["Prefix"]


def _invoke_worker(payload):
    lam.invoke(FunctionName=WORKER, InvocationType="Event", Payload=json.dumps(payload).encode())


# -------------------------------------------------------------------- worker

def worker(event, context):
    signal, dt, hour = event["signal"], event["dt"], event["hour"]
    if signal != "logs":
        raise ValueError(f"no compactor for signal {signal!r} yet")
    crash_after = event.get("crash_after") if ALLOW_CRASH_INJECTION else None
    lease = _acquire_lease(f"_lease#{signal}#{dt}#{hour}", context)
    if lease is None:
        return _done(signal, dt, hour, skipped="another worker holds this partition")
    try:
        return _compact_partition(signal, dt, hour, crash_after)
    finally:
        _release_lease(*lease)


def _compact_partition(signal, dt, hour, crash_after):
    manifest_pk = f"_manifest#{signal}#{dt}#{hour}"
    prefix = f"_incoming/{signal}/dt={dt}/hour={hour}/"

    finished = _finish_committed(manifest_pk)

    objects = _list(prefix)
    if not objects:
        return _done(signal, dt, hour, finished_batches=finished, inputs=0)
    chunk = _take_chunk(objects)
    keys = [o["Key"] for o in chunk]
    batch_id = hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]

    work = tempfile.mkdtemp(dir="/tmp")
    try:
        # 2. Parquet
        local = []
        for i, k in enumerate(keys):
            p = os.path.join(work, "in", f"{i:05d}.json.gz")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            s3.download_file(BUCKET, k, p)
            local.append(p)
        mem_mb = int(os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "3008"))
        written = compact.compact_logs(
            local, os.path.join(work, "out"), batch_id, dt, hour,
            memory_limit=f"{int(mem_mb * 0.6)}MB",
        )
        for w in written:
            w["key"] = f"{signal}/{w['relpath']}"
            s3.upload_file(w["path"], BUCKET, w["key"])
        _maybe_crash(crash_after, "write")

        # 3. index
        now = datetime.now(timezone.utc).isoformat()
        _batch_write([{
            "pk": {"S": f"{signal}#{w['service']}"},
            "sk": {"S": f"{w['min_ts']}#{batch_id}"},
            "min_ts": {"S": w["min_ts"]},
            "max_ts": {"S": w["max_ts"]},
            "file_path": {"S": f"s3://{BUCKET}/{w['key']}"},
            "row_count": {"N": str(w["rows"])},
            "size_bytes": {"N": str(w["size_bytes"])},
            "storage_class": {"S": "STANDARD"},
            "batch_id": {"S": batch_id},
            "compacted_at": {"S": now},
        } for w in written])
        _maybe_crash(crash_after, "index")

        # 4. commit
        ddb.put_item(TableName=TABLE, Item={
            "pk": {"S": manifest_pk}, "sk": {"S": batch_id},
            "inputs": {"L": [{"S": k} for k in keys]}, "committed_at": {"S": now},
        })
        _maybe_crash(crash_after, "commit")

        # 5-6. clean up
        if crash_after == "partial_delete":
            _delete_keys(keys[: len(keys) // 2])
            _maybe_crash(crash_after, "partial_delete")
        _delete_keys(keys)
        ddb.delete_item(TableName=TABLE, Key={"pk": {"S": manifest_pk}, "sk": {"S": batch_id}})
    finally:
        shutil.rmtree(work, ignore_errors=True)

    remaining = len(objects) - len(chunk)
    if remaining:
        _invoke_worker({"signal": signal, "dt": dt, "hour": hour})
    return _done(
        signal, dt, hour, batch_id=batch_id, inputs=len(keys),
        input_bytes=sum(o["Size"] for o in chunk), remaining=remaining, finished_batches=finished,
        outputs=[{k: w[k] for k in ("key", "rows", "size_bytes", "min_ts", "max_ts")} for w in written],
    )


def _acquire_lease(pk, context):
    """One worker per partition at a time. The lease outlives the function
    timeout, so a crashed holder's lease expires before anyone could still
    be running under it."""
    owner = getattr(context, "aws_request_id", "local")
    now = int(time.time())
    try:
        ddb.put_item(
            TableName=TABLE,
            Item={"pk": {"S": pk}, "sk": {"S": "lease"}, "owner": {"S": owner},
                  "expires_at": {"N": str(now + LEASE_SECONDS)}},
            ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
            ExpressionAttributeValues={":now": {"N": str(now)}},
        )
    except ddb.exceptions.ConditionalCheckFailedException:
        return None
    return pk, owner


def _release_lease(pk, owner):
    try:
        ddb.delete_item(
            TableName=TABLE, Key={"pk": {"S": pk}, "sk": {"S": "lease"}},
            ConditionExpression="#o = :o", ExpressionAttributeNames={"#o": "owner"},
            ExpressionAttributeValues={":o": {"S": owner}},
        )
    except ddb.exceptions.ConditionalCheckFailedException:
        pass


def _finish_committed(manifest_pk):
    """Step 1: delete inputs of batches that were committed but not cleaned up."""
    finished = []
    for page in ddb.get_paginator("query").paginate(
        TableName=TABLE, KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": manifest_pk}},
    ):
        for item in page["Items"]:
            _delete_keys([v["S"] for v in item["inputs"]["L"]])
            ddb.delete_item(TableName=TABLE, Key={"pk": item["pk"], "sk": item["sk"]})
            finished.append(item["sk"]["S"])
    return finished


def _list(prefix):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        out.extend(page.get("Contents", []))
    return sorted(out, key=lambda o: o["Key"])


def _take_chunk(objects):
    chunk, size = [], 0
    for o in objects:
        if chunk and (len(chunk) >= MAX_INPUT_FILES or size + o["Size"] > MAX_INPUT_BYTES):
            break
        chunk.append(o)
        size += o["Size"]
    return chunk


def _delete_keys(keys):
    for i in range(0, len(keys), 1000):
        resp = s3.delete_objects(
            Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True}
        )
        if resp.get("Errors"):
            raise RuntimeError(f"delete failed: {resp['Errors'][:3]}")


def _batch_write(items):
    for i in range(0, len(items), 25):
        pending = [{"PutRequest": {"Item": it}} for it in items[i:i + 25]]
        for attempt in range(8):
            resp = ddb.batch_write_item(RequestItems={TABLE: pending})
            pending = resp.get("UnprocessedItems", {}).get(TABLE, [])
            if not pending:
                break
            time.sleep(min(2 ** attempt * 0.1, 5))
        else:
            raise RuntimeError(f"{len(pending)} index writes left unprocessed")


def _maybe_crash(crash_after, step):
    if crash_after == step:
        raise RuntimeError(f"injected crash after step '{step}'")


def _done(signal, dt, hour, **kw):
    result = {"signal": signal, "dt": dt, "hour": hour, **kw}
    print(json.dumps(result))
    return result
