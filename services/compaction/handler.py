"""Compaction Lambdas.

Paths and keys are per tenant; see layout.py.

dispatcher: on a schedule, splits each tenant's closed _incoming/ hours into chunks,
            records a plan per chunk, and invokes one worker per plan, so a
            busy hour is compacted by many workers in parallel.
worker:     compacts one planned chunk into Parquet, indexes it, then deletes
            the chunk's raw files. Logs, traces and metrics alike
            (COMPACT_SIGNALS); see compact.py for each one's rows.

A plan fixes a chunk's input keys up front, and its batch_id is a hash of
them, so every retry of a chunk works on exactly the same inputs and writes
the same output keys.

recent_indexer: on each new raw file (S3 event via EventBridge), indexes it
            as kind=raw entries, one per (service, event hour), so lookups
            can return data within about a minute of arrival.

Worker steps, in this order, so any crash can be re-run safely:
  0. take a lease on the plan, so only one worker runs per chunk
  1. write Parquet   part-<batch_id>-NNN.parquet  (same inputs -> same keys)
  2. write index     sk = <min_ts>#<batch_id>-NNN (same inputs -> same keys),
                     and the chunk's usage record (key: tenant, day, signal, batch_id)
  3. commit          mark the plan committed
  4. retire the inputs' raw index entries
  5. delete inputs
  6. delete the plan
A crash before 3 redoes 1-2, overwriting the same keys. A crash after 3 skips
straight to 4. The dispatcher re-invokes any plan still present, so a
crashed chunk is retried on the next run. (A crash between 5 and 6 leaves
a plan whose inputs are all gone; it holds no data and is harmless.)

Handover between the fast lane and Parquet is step 3, a single write.
Lookups show a chunk's Parquet entries only once its plan is committed (or
gone), and hide raw entries whose file is in a committed plan, so a query
never counts a record twice or misses it (see lookup.py).
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
import botocore.exceptions

import compact
import dayfilter
import layout

BUCKET = os.environ["BUCKET"]
TABLE = os.environ["INDEX_TABLE"]
USAGE_TABLE = os.environ.get("USAGE_TABLE", "")  # per-tenant metering (T5)
WORKER = os.environ.get("WORKER_FUNCTION", "")
SIGNALS = [s for s in os.environ.get("COMPACT_SIGNALS", "logs").split(",") if s]
GRACE = timedelta(minutes=int(os.environ.get("GRACE_MINUTES", "10")))
# A plan item lists its input keys and must stay under DynamoDB's 400 KB
# item limit: 2000 keys of ~90 bytes is ~180 KB.
MAX_INPUT_FILES = int(os.environ.get("MAX_INPUT_FILES", "2000"))
MAX_INPUT_BYTES = int(os.environ.get("MAX_INPUT_BYTES", str(200 * 1024 * 1024)))
MAX_ROWS_PER_FILE = int(os.environ.get("MAX_ROWS_PER_FILE", str(compact.DEFAULT_MAX_ROWS_PER_FILE)))
BLOOM_ATTRIBUTES = tuple(a for a in os.environ.get(
    "BLOOM_ATTRIBUTES", ",".join(compact.DEFAULT_BLOOM_ATTRIBUTES)).split(",") if a)
# Blooms up to this size go in the index item itself; bigger ones go to S3
# next to the data. Kept small because a lookup reads whole index items
# (DynamoDB bills and pages by item size), so large inline blooms would make
# every time-range lookup slow and costly. 1 KB holds ~850 IDs.
BLOOM_INLINE_MAX_BYTES = int(os.environ.get("BLOOM_INLINE_MAX_BYTES", "1024"))
ALLOW_CRASH_INJECTION = os.environ.get("ALLOW_CRASH_INJECTION") == "true"
WORKER_LEASE_SECONDS = 16 * 60      # longer than the worker's 15-minute maximum timeout
DISPATCHER_LEASE_SECONDS = 6 * 60   # longer than the dispatcher's 5-minute timeout
LIST_THREADS = 16

s3 = boto3.client("s3")
ddb = boto3.client("dynamodb")
lam = boto3.client("lambda")
cw = boto3.client("cloudwatch")


# ---------------------------------------------------------------- dispatcher

def dispatcher(event, context):
    """Scheduled run: plan and invoke every closed hour.

    Test hook: {"plan_only": {"tenant", "signal", "dt", "hour"}} plans that
    one hour, ignoring the grace period, and returns its batch ids without
    invoking workers.
    """
    only = (event or {}).get("plan_only")
    if only:
        layout.check_tenant(only["tenant"])
    now = datetime.now(timezone.utc)
    result = {"planned": [], "invoked": []}
    for signal in [only["signal"]] if only else SIGNALS:
        # Two overlapping dispatchers could chunk the same keys differently
        # and compact some twice, so only one plans a signal at a time.
        lease = _acquire_lease(f"_lease#dispatcher#{signal}", context, DISPATCHER_LEASE_SECONDS)
        if lease is None:
            result.setdefault("skipped", []).append(signal)
            continue
        try:
            if only:
                hours = [(only["tenant"], only["dt"], only["hour"])]
            else:
                partitions = list(_incoming_partitions(signal))
                _put_oldest_age_metric(signal, now, partitions)
                hours = [(t, dt, hr) for t, dt, hr, start in partitions
                         if now >= start + timedelta(hours=1) + GRACE]
            for tenant, dt, hour in hours:
                batch_ids = _plan_hour(tenant, signal, dt, hour, now)
                result["planned"].extend(batch_ids)
                if not only:
                    for b in batch_ids:
                        _invoke_worker({"tenant": tenant, "signal": signal, "dt": dt, "hour": hour,
                                        "batch_id": b})
                        result["invoked"].append(b)
        finally:
            _release_lease(*lease)
    print(json.dumps(result))
    return result


def _plan_hour(tenant, signal, dt, hour, now):
    """Plan chunks for any unplanned raw files in the hour. Returns every
    plan for the hour, new and existing (existing ones are retried)."""
    pk = layout.plan_pk(tenant, signal, dt, hour)
    existing = _query(pk)
    planned_keys = {v["S"] for item in existing for v in item["inputs"]["L"]}
    batch_ids = [item["sk"]["S"] for item in existing]

    fresh = [o for o in _list(layout.incoming_prefix(tenant, signal, dt, hour)) if o["Key"] not in planned_keys]
    for chunk in _chunks(fresh):
        keys = [o["Key"] for o in chunk]
        batch_id = hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]
        try:
            ddb.put_item(
                TableName=TABLE,
                Item={"pk": {"S": pk}, "sk": {"S": batch_id}, "status": {"S": "planned"},
                      "inputs": {"L": [{"S": k} for k in keys]},
                      "input_bytes": {"N": str(sum(o["Size"] for o in chunk))},
                      "planned_at": {"S": now.isoformat()}},
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ddb.exceptions.ConditionalCheckFailedException:
            pass  # identical chunk already planned
        batch_ids.append(batch_id)
    return batch_ids


def _chunks(objects):
    chunk, size = [], 0
    for o in objects:
        if chunk and (len(chunk) >= MAX_INPUT_FILES or size + o["Size"] > MAX_INPUT_BYTES):
            yield chunk
            chunk, size = [], 0
        chunk.append(o)
        size += o["Size"]
    if chunk:
        yield chunk


def _put_oldest_age_metric(signal, now, partitions):
    """Across all tenants: one stuck tenant is enough to alarm."""
    oldest = min((p[-1] for p in partitions), default=None)
    cw.put_metric_data(
        Namespace="obs",
        MetricData=[{
            "MetricName": "OldestIncomingAgeMinutes",
            "Dimensions": [{"Name": "signal", "Value": signal}],
            "Value": (now - oldest).total_seconds() / 60 if oldest else 0, "Unit": "None",
        }],
    )


def _incoming_partitions(signal):
    """(tenant, dt, hour, hour start) for every raw hour of every tenant.
    Tenants are listed in parallel: each needs a few LIST calls, and there
    may be hundreds of tenants."""
    tenants = []
    for tenant_prefix in _common_prefixes("_incoming/"):
        tenant = tenant_prefix.rstrip("/").rsplit("tenant=", 1)[-1]
        try:
            tenants.append(layout.check_tenant(tenant))
        except ValueError:
            print(json.dumps({"skipped_prefix": tenant_prefix}))  # not a tenant=<T>/ folder

    def hours_of(tenant):
        out = []
        for dt_prefix in _common_prefixes(layout.incoming_prefix(tenant, signal)):
            dt = dt_prefix.rstrip("/").rsplit("dt=", 1)[1]
            for hour_prefix in _common_prefixes(dt_prefix):
                hour = hour_prefix.rstrip("/").rsplit("hour=", 1)[1]
                start = datetime.strptime(f"{dt} {hour}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc)
                out.append((tenant, dt, hour, start))
        return out

    with ThreadPoolExecutor(LIST_THREADS) as pool:
        for hours in pool.map(hours_of, tenants):
            yield from hours


def _common_prefixes(prefix):
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            yield p["Prefix"]


def _invoke_worker(payload):
    lam.invoke(FunctionName=WORKER, InvocationType="Event", Payload=json.dumps(payload).encode())


# -------------------------------------------------------------------- worker

def worker(event, context):
    tenant = layout.check_tenant(event["tenant"])
    signal, dt, hour, batch_id = event["signal"], event["dt"], event["hour"], event["batch_id"]
    if signal not in compact.SIGNALS:
        raise ValueError(f"no compactor for signal {signal!r}")
    crash_after = event.get("crash_after") if ALLOW_CRASH_INJECTION else None
    lease = _acquire_lease(layout.worker_lease_pk(tenant, signal, dt, hour, batch_id), context,
                           WORKER_LEASE_SECONDS)
    if lease is None:
        return _done(batch_id, skipped="another worker holds this chunk")
    try:
        return _compact_chunk(tenant, signal, dt, hour, batch_id, crash_after)
    finally:
        _release_lease(*lease)


def _compact_chunk(tenant, signal, dt, hour, batch_id, crash_after):
    key = {"pk": {"S": layout.plan_pk(tenant, signal, dt, hour)}, "sk": {"S": batch_id}}
    plan = ddb.get_item(TableName=TABLE, Key=key, ConsistentRead=True).get("Item")
    if plan is None:
        return _done(batch_id, skipped="no such plan (already finished)")
    keys = [v["S"] for v in plan["inputs"]["L"]]
    resumed = plan["status"]["S"] == "committed"

    written = []
    if not resumed:
        work = tempfile.mkdtemp(dir="/tmp")
        try:
            # 1. Parquet
            local = []
            for i, k in enumerate(keys):
                p = os.path.join(work, "in", f"{i:05d}.json.gz")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                s3.download_file(BUCKET, k, p)
                local.append(p)
            mem_mb = int(os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "3008"))
            digests = {}
            written = compact.compact(
                signal, local, os.path.join(work, "out"), batch_id, dt, hour,
                memory_limit=f"{int(mem_mb * 0.6)}MB", max_rows_per_file=MAX_ROWS_PER_FILE,
                bloom_attributes=BLOOM_ATTRIBUTES, id_digests=digests,
            )
            prefix = layout.data_prefix(tenant, signal)
            for w in written:
                w["key"] = prefix + w["relpath"]
                s3.upload_file(w["path"], BUCKET, w["key"])
                bits = w["bloom"].to_bytes()
                if len(bits) > BLOOM_INLINE_MAX_BYTES:
                    w["bloom_key"] = f"{prefix}_bloom/{batch_id}-{w['part']:03d}-{w['service']}.bloom"
                    s3.put_object(Bucket=BUCKET, Key=w["bloom_key"], Body=bits)
            # ID digests for the day filters (same keys on a re-run).
            for day, groups in digests.items():
                for g, buf in groups.items():
                    s3.put_object(Bucket=BUCKET, Key=layout.ids_key(tenant, signal, day, g, batch_id), Body=bytes(buf))
            _maybe_crash(crash_after, "write")

            # 2. index. First mark the days this chunk adds to as changed, so a
            # day filter sealed without this chunk's IDs is no longer trusted
            # (see sealer).
            for day in digests:
                # fieldsets: which ID fields the digests cover; lookups use a day
                # filter only if every chunk of the day covered the same fields.
                ddb.update_item(TableName=TABLE, Key={"pk": {"S": layout.day_pk(tenant, signal)}, "sk": {"S": day}},
                                UpdateExpression="ADD dirty :one, fieldsets :fs",
                                ExpressionAttributeValues={":one": {"N": "1"}, ":fs": {"SS": [",".join(
                                    compact.bloom_fields(signal, BLOOM_ATTRIBUTES))]}})
            now = datetime.now(timezone.utc).isoformat()
            plan_pk = layout.plan_pk(tenant, signal, dt, hour)
            _batch_write([_index_item(tenant, signal, batch_id, w, now, plan_pk) for w in written])
            if written:  # registry of services, for lookups across all of them
                ddb.update_item(
                    TableName=TABLE, Key={"pk": {"S": layout.services_pk(tenant, signal)}, "sk": {"S": "all"}},
                    UpdateExpression="ADD services :s",
                    ExpressionAttributeValues={":s": {"SS": sorted({w["service"] for w in written})}},
                )
            if USAGE_TABLE:
                # Written before the commit, under a key fixed by the chunk: a
                # re-run overwrites it, so each chunk is counted exactly once.
                ddb.put_item(TableName=USAGE_TABLE, Item={
                    "tenant": {"S": tenant}, "sk": {"S": f"{dt}#{signal}#{batch_id}"},
                    "dt": {"S": dt}, "hour": {"S": hour}, "signal": {"S": signal},
                    "records": {"N": str(sum(w["rows"] for w in written))},
                    "raw_bytes": {"N": str(int(plan.get("input_bytes", {}).get("N", "0")))},
                    "stored_bytes": {"N": str(sum(w["size_bytes"] for w in written))},
                    "files": {"N": str(len(written))}, "inputs": {"N": str(len(keys))},
                    "compacted_at": {"S": now},
                })
            _maybe_crash(crash_after, "index")
        finally:
            shutil.rmtree(work, ignore_errors=True)

        # 3. commit
        ddb.update_item(
            TableName=TABLE, Key=key, UpdateExpression="SET #s = :c",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":c": {"S": "committed"}},
        )
        _maybe_crash(crash_after, "commit")

    # 4. retire the raw (fast lane) index entries of the inputs
    _retire_raw_entries(tenant, signal, dt, hour, keys)
    _maybe_crash(crash_after, "retire")

    # 5-6. clean up
    if crash_after == "partial_delete":
        _delete_keys(keys[: len(keys) // 2])
        _maybe_crash(crash_after, "partial_delete")
    _delete_keys(keys)
    ddb.delete_item(TableName=TABLE, Key=key)

    return _done(
        batch_id, tenant=tenant, signal=signal, dt=dt, hour=hour, inputs=len(keys), resumed=resumed,
        outputs=[{k: w[k] for k in ("key", "rows", "size_bytes", "min_ts", "max_ts")} for w in written],
    )


# ---------------------------------------------------------------- fast lane

def recent_indexer(event, context):
    """Index a new raw file (EventBridge "Object Created") as kind=raw entries.

    Writes the raw-file record (listing the entries) before the entries, so
    compaction can always find and retire them. Idempotent: the same file
    always produces the same keys."""
    detail = event.get("detail") or {}
    key = urllib.parse.unquote_plus((detail.get("object") or {}).get("key", ""))
    parsed = layout.parse_incoming_key(key)
    if parsed is None:
        return _done(None, skipped=f"not a raw file: {key}")
    tenant, signal, dt, hour = parsed
    if signal not in compact.SIGNALS:
        return _done(None, skipped=f"unknown signal {signal!r}: {key}")

    work = tempfile.mkdtemp(dir="/tmp")
    try:
        local = os.path.join(work, "raw.json.gz")
        try:
            s3.download_file(BUCKET, key, local)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return _done(None, skipped=f"already compacted: {key}")
            raise
        size = os.path.getsize(local)
        mem_mb = int(os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "2048"))
        groups = compact.summarize(signal, [local], work, dt, hour, memory_limit=f"{int(mem_mb * 0.6)}MB",
                                   bloom_attributes=BLOOM_ATTRIBUTES)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    fid = hashlib.sha256(key.encode()).hexdigest()[:16]
    plan_pk = layout.plan_pk(tenant, signal, dt, hour)
    items = []
    for i, g in enumerate(groups):
        item = {
            "pk": {"S": layout.index_pk(tenant, signal, g["service"])},
            "sk": {"S": f"{g['min_ts']}#raw#{fid}-{i:03d}"},
            "kind": {"S": "raw"},
            "plan_pk": {"S": plan_pk},
            "raw_key": {"S": key},
            "min_ts": {"S": g["min_ts"]},
            "max_ts": {"S": g["max_ts"]},
            "file_path": {"S": f"s3://{BUCKET}/{key}"},
            "row_count": {"N": str(g["rows"])},
            "size_bytes": {"N": str(size)},  # of the whole raw file, which may hold several groups
            "storage_class": {"S": "STANDARD"},
            "indexed_at": {"S": datetime.now(timezone.utc).isoformat()},
            "bloom_m": {"N": str(g["bloom"].m)},
            "bloom_k": {"N": str(g["bloom"].k)},
            "bloom_n": {"N": str(g["bloom"].n)},
            "bloom_fields": {"L": [{"S": f} for f in compact.bloom_fields(signal, BLOOM_ATTRIBUTES)]},
        }
        bits = g["bloom"].to_bytes()
        if len(bits) > BLOOM_INLINE_MAX_BYTES:
            bkey = f"{layout.data_prefix(tenant, signal)}_bloom/raw-{fid}-{i:03d}.bloom"
            s3.put_object(Bucket=BUCKET, Key=bkey, Body=bits)
            item["bloom_s3_key"] = {"S": bkey}
        else:
            item["bloom"] = {"B": bits}
        items.append(item)

    ddb.put_item(TableName=TABLE, Item={
        "pk": {"S": layout.raw_files_pk(tenant, signal, dt, hour)}, "sk": {"S": key},
        "entries": {"L": [{"M": {"pk": it["pk"], "sk": it["sk"]}} for it in items]},
        # bloom objects of those entries, deleted with them
        "blooms": {"L": [{"S": it["bloom_s3_key"]["S"]} for it in items if "bloom_s3_key" in it]},
    })
    _batch_write(items)
    if items:
        ddb.update_item(
            TableName=TABLE, Key={"pk": {"S": layout.services_pk(tenant, signal)}, "sk": {"S": "all"}},
            UpdateExpression="ADD services :s",
            ExpressionAttributeValues={":s": {"SS": sorted({g["service"] for g in groups})}},
        )
    return _done(None, tenant=tenant, key=key, entries=len(items), rows=sum(g["rows"] for g in groups))


# -------------------------------------------------------------- day filters

SEAL_AFTER = timedelta(hours=int(os.environ.get("SEAL_AFTER_HOURS", "2")))  # after the day ends
KEEP_IDS = timedelta(days=int(os.environ.get("KEEP_IDS_DAYS", "3")))        # digests kept for re-sealing
SEAL_LEASE_SECONDS = 16 * 60


def sealer(event, context):
    """Hourly: build the day filter of every closed day whose filter is
    missing or out of date, and delete old digests.

    Consistency: a worker writes its digests, then bumps the day's `dirty`
    counter, then writes its index items. The sealer reads `dirty`, lists
    and merges the digests, and records `sealed = dirty` only if `dirty` is
    unchanged. Lookups trust a day filter only while sealed == dirty, so a
    chunk added during or after sealing makes them fall back to per-file
    blooms for that day until the next seal.

    Test hook: {"only": {"tenant", "signal", "dt"}} seals that day now.
    """
    now = datetime.now(timezone.utc)
    deadline = time.time() + (context.get_remaining_time_in_millis() / 1000 - 90 if context else 600)
    only = (event or {}).get("only")
    if only:
        days = [(layout.check_tenant(only["tenant"]), only["signal"], only["dt"])]
    else:
        days = [d for d in _days_with_ids() if now >= _day_end(d[2]) + SEAL_AFTER]
    result = {"sealed": [], "up_to_date": 0, "ids_deleted": [], "skipped": []}
    for tenant, signal, day in days:
        if time.time() > deadline:
            result["skipped"].append(f"{tenant}/{signal}/{day}")
            continue
        outcome = _seal_day(tenant, signal, day, now, context, force=bool(only))
        if outcome == "sealed":
            result["sealed"].append(f"{tenant}/{signal}/{day}")
        elif outcome == "ids_deleted":
            result["ids_deleted"].append(f"{tenant}/{signal}/{day}")
        else:
            result["up_to_date"] += 1
    print(json.dumps(result))
    return result


def _day_end(day):
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)


def _days_with_ids():
    """(tenant, signal, day) for every day that still has digests."""
    out = []
    for tenant_prefix in _common_prefixes("data/"):
        tenant = tenant_prefix.rstrip("/").rsplit("tenant=", 1)[-1]
        try:
            layout.check_tenant(tenant)
        except ValueError:
            continue
        for signal in compact.SIGNALS:
            for p in _common_prefixes(f"{layout.data_prefix(tenant, signal)}_ids/"):
                out.append((tenant, signal, p.rstrip("/").rsplit("dt=", 1)[1]))
    return out


def _seal_day(tenant, signal, day, now, context, force=False):
    key = {"pk": {"S": layout.day_pk(tenant, signal)}, "sk": {"S": day}}
    item = ddb.get_item(TableName=TABLE, Key=key, ConsistentRead=True).get("Item", {})
    dirty = int(item.get("dirty", {}).get("N", "0"))
    sealed = int(item.get("sealed", {}).get("N", "-1"))
    if dirty == 0 or item.get("ids_deleted"):
        return "nothing to seal"   # no committed chunk yet, or digests gone (per-file blooms serve it)
    if sealed == dirty:
        if now >= _day_end(day) + KEEP_IDS and not force:
            # Re-sealing needs every digest, so from here on a late chunk leaves
            # the day on per-file blooms instead.
            ddb.update_item(TableName=TABLE, Key=key, UpdateExpression="SET ids_deleted = :t",
                            ExpressionAttributeValues={":t": {"BOOL": True}})
            _delete_keys([o["Key"] for o in _list(layout.ids_prefix(tenant, signal, day))])
            return "ids_deleted"
        return "up to date"
    lease = _acquire_lease(f"_lease#seal#{tenant}#{signal}#{day}", context, SEAL_LEASE_SECONDS)
    if lease is None:
        return "another sealer is on it"
    try:
        by_group = {}
        for o in _list(layout.ids_prefix(tenant, signal, day)):
            g = int(o["Key"].rsplit("/g=", 1)[1].split("/", 1)[0])
            by_group.setdefault(g, []).append(o["Key"])
        groups, n_total = {}, 0
        for g in range(dayfilter.GROUPS):
            buf = bytearray()
            for k in by_group.get(g, []):
                buf.extend(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())
            subshards, m, n, bits = dayfilter.build_group(bytes(buf))
            s3.put_object(Bucket=BUCKET, Key=layout.day_filter_key(tenant, signal, day, dirty, g), Body=bits)
            groups[str(g)] = {"M": {"s": {"N": str(subshards)}, "m": {"N": str(m)}}}
            n_total += n
        try:
            ddb.update_item(
                TableName=TABLE, Key=key, ConditionExpression="dirty = :v",
                UpdateExpression="SET sealed = :v, #g = :g, ids = :n, sealed_at = :t",
                ExpressionAttributeNames={"#g": "groups"},
                ExpressionAttributeValues={":v": {"N": str(dirty)}, ":g": {"M": groups},
                                           ":n": {"N": str(n_total)}, ":t": {"S": now.isoformat()}})
        except ddb.exceptions.ConditionalCheckFailedException:
            return "changed while sealing; next run"
        # Older versions of this day's filter are no longer referenced.
        old = [o["Key"] for o in _list(layout.day_filter_prefix(tenant, signal, day))
               if f"/v={dirty}/" not in o["Key"]]
        _delete_keys(old)
        return "sealed"
    finally:
        _release_lease(*lease)


def _retire_raw_entries(tenant, signal, dt, hour, keys):
    """Delete the fast-lane entries (and raw-file records) of these raw files."""
    pk = layout.raw_files_pk(tenant, signal, dt, hour)
    for i in range(0, len(keys), 100):
        req = {TABLE: {"Keys": [{"pk": {"S": pk}, "sk": {"S": k}} for k in keys[i:i + 100]],
                       "ConsistentRead": True}}
        while req:
            resp = ddb.batch_get_item(RequestItems=req)
            for rec in resp.get("Responses", {}).get(TABLE, []):
                entries = [e["M"] for e in rec.get("entries", {}).get("L", [])]
                _batch_delete([{"pk": e["pk"], "sk": e["sk"]} for e in entries])
                _delete_keys([b["S"] for b in rec.get("blooms", {}).get("L", [])])
                ddb.delete_item(TableName=TABLE, Key={"pk": rec["pk"], "sk": rec["sk"]})
            req = resp.get("UnprocessedKeys") or None


def _batch_delete(keys):
    for i in range(0, len(keys), 25):
        pending = [{"DeleteRequest": {"Key": k}} for k in keys[i:i + 25]]
        for attempt in range(8):
            resp = ddb.batch_write_item(RequestItems={TABLE: pending})
            pending = resp.get("UnprocessedItems", {}).get(TABLE, [])
            if not pending:
                break
            time.sleep(min(2 ** attempt * 0.1, 5))
        else:
            raise RuntimeError(f"{len(pending)} index deletes left unprocessed")


# ------------------------------------------------------------------- helpers

def _index_item(tenant, signal, batch_id, w, now, plan_pk):
    b = w["bloom"]
    item = {
        "pk": {"S": layout.index_pk(tenant, signal, w["service"])},
        "sk": {"S": f"{w['min_ts']}#{batch_id}-{w['part']:03d}"},
        "kind": {"S": "parquet"},
        # Lookups hide this entry while its plan is still "planned" (the
        # raw files it replaces are still the visible copy).
        "plan_pk": {"S": plan_pk},
        "min_ts": {"S": w["min_ts"]},
        "max_ts": {"S": w["max_ts"]},
        "file_path": {"S": f"s3://{BUCKET}/{w['key']}"},
        "row_count": {"N": str(w["rows"])},
        "size_bytes": {"N": str(w["size_bytes"])},
        "storage_class": {"S": "STANDARD"},
        "batch_id": {"S": batch_id},
        "compacted_at": {"S": now},
        "bloom_m": {"N": str(b.m)},
        "bloom_k": {"N": str(b.k)},
        "bloom_n": {"N": str(b.n)},
        "bloom_fields": {"L": [{"S": f} for f in compact.bloom_fields(signal, BLOOM_ATTRIBUTES)]},
    }
    if "bloom_key" in w:
        item["bloom_s3_key"] = {"S": w["bloom_key"]}
    else:
        item["bloom"] = {"B": b.to_bytes()}
    return item


def _acquire_lease(pk, context, seconds):
    """Conditional put: succeeds only if nobody holds the lease or it has
    expired. Leases outlive the holder's timeout, so a crashed holder's lease
    expires before anyone could still be running under it."""
    owner = getattr(context, "aws_request_id", "local")
    now = int(time.time())
    try:
        ddb.put_item(
            TableName=TABLE,
            Item={"pk": {"S": pk}, "sk": {"S": "lease"}, "owner": {"S": owner},
                  "expires_at": {"N": str(now + seconds)}},
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


def _query(pk):
    items = []
    for page in ddb.get_paginator("query").paginate(
        TableName=TABLE, KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": pk}}, ConsistentRead=True,
    ):
        items.extend(page["Items"])
    return items


def _list(prefix):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        out.extend(page.get("Contents", []))
    return sorted(out, key=lambda o: o["Key"])


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


def _done(batch_id, **kw):
    result = {"batch_id": batch_id, **kw}
    print(json.dumps(result))
    return result
