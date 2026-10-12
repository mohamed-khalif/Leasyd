#!/usr/bin/env python3
"""Soak and fault tests: an exact ledger next to the T6 load.

  soak.py setup                       create the ledger tenant qa-soak-1 (key saved to ~/.obs-soak.json)
  soak.py send --hours 12             every 15 s: 50 logs, 50 spans, 10 metric points, each with a
                                      unique soak.seq; the accepted counts go to ~/.obs-soak-ledger.jsonl
  soak.py watch --hours 12            every 5 min: a 1-hour query, a 24-hour query, the fast-lane DLQ,
                                      alarms, errors and memory of every obs-* function (~/.obs-soak-watch.jsonl)
  soak.py verify [--since ISO]        per hour: records the ledger says were accepted vs records stored,
                                      and distinct soak.seq (duplicates), per signal
  On the soak customer (infra/soak-customer.yaml) send runs there, with SOAK_KEY, SOAK_LEDGER and
  SOAK_ENDPOINT set, and its ledger is copied to S3 every 5 minutes.

  soak.py teardown                    delete the ledger tenant

Run next to `loadtest.py run --step soak1`, which supplies the volume across the 100 t6 tenants.
The ledger tenant is small on purpose: its numbers must come out exact, with or without faults.
"""

import argparse
import json
import os
import random
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config

REGION = "us-east-1"
TENANT = "qa-soak-1"
CREDS = os.path.expanduser("~/.obs-soak.json")
LEDGER = os.environ.get("SOAK_LEDGER") or os.path.expanduser("~/.obs-soak-ledger.jsonl")
WATCH = os.path.expanduser("~/.obs-soak-watch.jsonl")
ENDPOINT = os.environ.get("SOAK_ENDPOINT", "https://2jiyjrkjv7.execute-api.us-east-1.amazonaws.com/ingest")   # = ingest.leasyd.com
SERVICE = "soak-ledger"
PER_BATCH = {"logs": 50, "traces": 50, "metrics": 10}
INTERVAL_S = 15
FUNCTIONS = ["obs-ingest", "obs-ingest-authorizer", "obs-recent-indexer", "obs-compaction-worker",
             "obs-compaction-dispatcher", "obs-index-lookup", "obs-query", "obs-query-worker", "obs-query-api"]

lam = boto3.client("lambda", region_name=REGION, config=botocore.config.Config(read_timeout=900, retries={"max_attempts": 0}))
cw = boto3.client("cloudwatch", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)


def now_utc():
    return datetime.now(timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def invoke(fn, payload):
    return json.loads(lam.invoke(FunctionName=fn, Payload=json.dumps(payload).encode())["Payload"].read())


def setup(_):
    assert TENANT.startswith("qa-")
    r = invoke("obs-tenant-admin", {"action": "create", "tenant": TENANT, "plan": "free"})
    with open(CREDS, "w") as f:
        json.dump({"tenant": TENANT, "key": r["api_key"]}, f)
    os.chmod(CREDS, 0o600)
    print("created", TENANT, "key id", r.get("key_id"))


def teardown(_):
    assert TENANT.startswith("qa-")
    print(invoke("obs-tenant-admin", {"action": "delete", "tenant": TENANT}))


# ------------------------------------------------------------------ send

def _attrs(d):
    return [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}} for k, v in d.items()]


def _body(signal, seqs, t_ns):
    res = {"attributes": _attrs({"service.name": SERVICE})}
    if signal == "logs":
        recs = [{"timeUnixNano": str(t_ns), "severityNumber": 9, "severityText": "INFO",
                 "body": {"stringValue": f"soak record {s}"}, "attributes": _attrs({"soak.seq": s})} for s in seqs]
        return {"resourceLogs": [{"resource": res, "scopeLogs": [{"logRecords": recs}]}]}
    if signal == "traces":
        spans = [{"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "name": "soak", "kind": 2,
                  "startTimeUnixNano": str(t_ns - 5_000_000), "endTimeUnixNano": str(t_ns),
                  "status": {"code": 1}, "attributes": _attrs({"soak.seq": s})} for s in seqs]
        return {"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": spans}]}]}
    pts = [{"timeUnixNano": str(t_ns), "asDouble": 1.0, "attributes": _attrs({"soak.seq": s})} for s in seqs]
    return {"resourceMetrics": [{"resource": res, "scopeMetrics": [{"metrics": [
        {"name": "soak.ledger", "gauge": {"dataPoints": pts}}]}]}]}


def _post(key, signal, body):
    """-> (status, rejected, attempts). Retries 429/5xx/network like an SDK exporter."""
    data = json.dumps(body).encode()
    for attempt in range(1, 7):
        req = urllib.request.Request(f"{ENDPOINT}/v1/{signal}", data=data, method="POST",
                                     headers={"x-api-key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read() or b"{}")
                ps = out.get("partialSuccess") or {}
                rej = int(next((v for k, v in ps.items() if k.startswith("rejected")), 0) or 0)
                return 200, rej, attempt
        except urllib.error.HTTPError as e:
            status = e.code
        except OSError:
            status = 0
        if 400 <= status < 500 and status != 429:
            return status, 0, attempt
        time.sleep(min(0.5 * 2 ** (attempt - 1), 8) * (0.5 + random.random()))
    return status, 0, 6


def send(a):
    key = os.environ.get("SOAK_KEY") or json.load(open(CREDS))["key"]
    end = time.time() + a.hours * 3600
    seq = int(time.time() * 1000) * 1000   # unique across restarts
    while time.time() < end:
        t0 = time.time()
        row = {"ts": iso(now_utc()), "t_ns": time.time_ns()}
        for signal, n in PER_BATCH.items():
            seqs = list(range(seq, seq + n))
            seq += n
            status, rej, attempts = _post(key, signal, _body(signal, seqs, row["t_ns"]))
            row[signal] = {"status": status, "accepted": n - rej if status == 200 else 0, "rejected": rej,
                           "attempts": attempts, "first": seqs[0], "last": seqs[-1]}
        with open(LEDGER, "a") as f:
            f.write(json.dumps(row) + "\n")
        time.sleep(max(0, INTERVAL_S - (time.time() - t0)))


# ------------------------------------------------------------------ watch

def _metric(ns, name, dims, stat, start, end, period=300):
    r = cw.get_metric_statistics(Namespace=ns, MetricName=name, Dimensions=dims, StartTime=start, EndTime=end,
                                 Period=period, Statistics=[stat])
    return sum(p[stat] for p in r["Datapoints"]) if stat == "Sum" else max((p[stat] for p in r["Datapoints"]), default=0)


def _max_memory(fn, start):
    """Highest 'Max Memory Used' in the function's REPORT lines since start (leaks show as a climb)."""
    try:
        q = logs.start_query(logGroupName=f"/aws/lambda/{fn}", startTime=int(start.timestamp()), endTime=int(time.time()),
                             queryString="filter @type = 'REPORT' | stats max(@maxMemoryUsed / 1000 / 1000) as mb")["queryId"]
        for _ in range(30):
            r = logs.get_query_results(queryId=q)
            if r["status"] == "Complete":
                return float(r["results"][0][0]["value"]) if r["results"] else None
            time.sleep(1)
    except Exception as e:   # noqa: BLE001
        return f"err {type(e).__name__}"
    return None


def _timed_sql(hours):
    t0 = time.time()
    end = now_utc()
    try:
        out = invoke("obs-query", {"tenant": TENANT, "sql": "select count(*) as n from logs",
                                   "start": iso(end - timedelta(hours=hours)), "end": iso(end)})
        return {"s": round(time.time() - t0, 2), "rows": out.get("rows"), "error": out.get("error") or out.get("errorMessage")}
    except Exception as e:   # noqa: BLE001
        return {"s": round(time.time() - t0, 2), "error": f"{type(e).__name__}: {e}"[:200]}


def watch(a):
    end = time.time() + a.hours * 3600
    dlq = sqs.get_queue_url(QueueName="obs-fastlane-failed")["QueueUrl"]
    while time.time() < end:
        t = now_utc()
        start = t - timedelta(minutes=5)
        row = {"ts": iso(t), "q1h": _timed_sql(1), "q24h": _timed_sql(24)}
        row["dlq"] = int(sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["ApproximateNumberOfMessages"])
                         ["Attributes"]["ApproximateNumberOfMessages"])
        row["alarms"] = [x["AlarmName"] for x in cw.describe_alarms(StateValue="ALARM")["MetricAlarms"]]
        row["fn"] = {}
        for fn in FUNCTIONS:
            d = [{"Name": "FunctionName", "Value": fn}]
            row["fn"][fn] = {"inv": _metric("AWS/Lambda", "Invocations", d, "Sum", start, t),
                             "err": _metric("AWS/Lambda", "Errors", d, "Sum", start, t),
                             "thr": _metric("AWS/Lambda", "Throttles", d, "Sum", start, t)}
        if a.memory:
            for fn in ("obs-ingest", "obs-recent-indexer", "obs-query-worker", "obs-compaction-worker"):
                row["fn"][fn]["mem_mb"] = _max_memory(fn, start)
        with open(WATCH, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        print(row["ts"], "q1h", row["q1h"].get("s"), "q24h", row["q24h"].get("s"), "dlq", row["dlq"],
              "alarms", row["alarms"], flush=True)
        time.sleep(max(0, 300 - (time.time() - t.timestamp())))


# ------------------------------------------------------------------ verify

SQL = {"logs": "select count(*) as n, count(distinct attributes['soak.seq']) as d from logs where service = '%s'",
       "traces": "select count(*) as n, count(distinct attributes['soak.seq']) as d from spans where service = '%s'",
       "metrics": "select count(*) as n, count(distinct attributes['soak.seq']) as d from metrics "
                  "where service = '%s' and metric_name = 'soak.ledger'"}


def verify(a):
    rows = [json.loads(x) for x in open(a.ledger or LEDGER)]
    since = a.since or rows[0]["ts"]
    rows = [r for r in rows if r["ts"] >= since]
    hours = {}
    for r in rows:
        h = r["ts"][:13]
        for s in PER_BATCH:
            e = hours.setdefault(h, {}).setdefault(s, {"accepted": 0, "failed": 0, "retried": 0})
            e["accepted"] += r[s]["accepted"]
            e["failed"] += r[s]["status"] != 200
            e["retried"] += r[s]["attempts"] > 1
    bad = 0
    for h in sorted(hours):
        start = datetime.strptime(h, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        end = min(start + timedelta(hours=1), now_utc())
        line = [h + "h"]
        for s in PER_BATCH:
            out = invoke("obs-query", {"tenant": TENANT, "sql": SQL[s] % SERVICE, "start": iso(start), "end": iso(end)})
            if out.get("error") or not out.get("rows"):
                line.append(f"{s}: query failed {out.get('error') or out}"[:120])
                bad += 1
                continue
            n, d = out["rows"][0][:2]
            e = hours[h][s]
            ok = n == d == e["accepted"]
            bad += not ok
            line.append(f"{s} sent {e['accepted']} stored {n} distinct {d}" + ("" if ok else " MISMATCH") +
                        (f" (failed {e['failed']}, retried {e['retried']})" if e["failed"] or e["retried"] else ""))
        print("  ".join(line), flush=True)
    print("ALL EXACT" if not bad else f"{bad} mismatches")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("teardown")
    s = sub.add_parser("send")
    s.add_argument("--hours", type=float, default=12)
    w = sub.add_parser("watch")
    w.add_argument("--hours", type=float, default=12)
    w.add_argument("--memory", action="store_true", help="also read peak memory from the functions' logs")
    v = sub.add_parser("verify")
    v.add_argument("--since")
    v.add_argument("--ledger", help="a ledger file, e.g. the soak customer's, copied from S3")
    a = p.parse_args()
    {"setup": setup, "teardown": teardown, "send": send, "watch": watch, "verify": verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
