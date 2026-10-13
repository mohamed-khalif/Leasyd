#!/usr/bin/env python3
"""Fault tests: pause one Leasyd function (reserved concurrency 0), watch, resume.

  faults.py run <function> --minutes N [--label L]

While paused, every 30 s: the alarms in ALARM, a 15-minute SQL query on the ledger tenant (how long
it takes, or its error) and the fast-lane dead-letter queue, as JSON lines in ~/.obs-faults.jsonl.
The function is always resumed (concurrency limit removed) at the end, also on Ctrl-C or an error;
it then keeps watching for --after minutes so recovery is recorded too.

Run `soak.py send` next to it for traffic with an exact ledger, then `soak.py verify` afterwards.
Needs infra/iam/deployer-faults.json on obs-deployer. Only functions named obs-* can be paused, and
only ones without a concurrency limit of their own (so resuming restores them exactly).
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config

REGION = "us-east-1"
LOG = os.path.expanduser("~/.obs-faults.jsonl")
TENANT = "qa-soak-1"

lam = boto3.client("lambda", region_name=REGION, config=botocore.config.Config(read_timeout=120, retries={"max_attempts": 0}))
cw = boto3.client("cloudwatch", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def probe_query():
    t0, end = time.time(), datetime.now(timezone.utc)
    try:
        out = json.loads(lam.invoke(FunctionName="obs-query", Payload=json.dumps({
            "tenant": TENANT, "sql": "select count(*) from logs",
            "start": iso(end - timedelta(minutes=15)), "end": iso(end)}).encode())["Payload"].read())
        err = out.get("error") or out.get("errorMessage")
        return {"s": round(time.time() - t0, 1), "rows": None if err else out.get("rows"), "error": (err or None) and str(err)[:160]}
    except Exception as e:   # noqa: BLE001
        return {"s": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}"[:160]}


def sample(label, phase, dlq):
    row = {"ts": iso(datetime.now(timezone.utc)), "label": label, "phase": phase,
           "alarms": sorted(a["AlarmName"] for a in cw.describe_alarms(StateValue="ALARM")["MetricAlarms"]),
           "dlq": int(sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["ApproximateNumberOfMessages"])
                      ["Attributes"]["ApproximateNumberOfMessages"]),
           "query": probe_query()}
    with open(LOG, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(row["ts"][11:19], phase, "alarms", row["alarms"], "dlq", row["dlq"], "query", row["query"], flush=True)


def run(a):
    fn = a.function
    assert fn.startswith("obs-"), "only obs-* functions"
    if lam.get_function_concurrency(FunctionName=fn).get("ReservedConcurrentExecutions") is not None:
        raise SystemExit(f"{fn} has its own concurrency limit; not touching it")
    label = a.label or fn
    dlq = sqs.get_queue_url(QueueName="obs-fastlane-failed")["QueueUrl"]
    sample(label, "before", dlq)
    lam.put_function_concurrency(FunctionName=fn, ReservedConcurrentExecutions=0)
    print(f"{iso(datetime.now(timezone.utc))} PAUSED {fn} for {a.minutes} min", flush=True)
    try:
        end = time.time() + a.minutes * 60
        while time.time() < end:
            time.sleep(min(30, max(0, end - time.time())))
            sample(label, "paused", dlq)
    finally:
        lam.delete_function_concurrency(FunctionName=fn)
        print(f"{iso(datetime.now(timezone.utc))} RESUMED {fn}", flush=True)
    end = time.time() + a.after * 60
    while time.time() < end:
        time.sleep(30)
        sample(label, "after", dlq)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("function")
    r.add_argument("--minutes", type=float, required=True)
    r.add_argument("--after", type=float, default=10, help="minutes to keep watching after resuming")
    r.add_argument("--label")
    a = p.parse_args()
    run(a)


if __name__ == "__main__":
    main()
