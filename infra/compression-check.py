#!/usr/bin/env python3
"""Proves the Firehose compression switch changes nothing a customer can see.

    python3 infra/compression-check.py send before     # before RecordCompression=gzip
    python3 infra/compression-check.py send after      # after it
    python3 infra/compression-check.py compare         # every field of every record must match

Sends the same 1,000 log records (fixed content, times and ids) through the
public endpoint as service "cmp-before" and later "cmp-after" for a load-test
tenant, then reads both back through obs-query and compares them row by row,
ignoring only the service name.
"""

import gzip
import json
import os
import random
import sys
import time
import urllib.request

import boto3
import botocore.config

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
KEYS = os.path.expanduser("~/.obs-t6-keys.json")
STATE = os.path.expanduser("~/.obs-compression-check.json")
TENANT = "t6-099"
N = 1000


def batch(service, base_ns):
    rnd = random.Random(42)   # the same content every time
    recs = [{"timeUnixNano": str(base_ns + i * 1_000_000), "severityNumber": rnd.choice([9, 13, 17]),
             "severityText": "x", "body": {"stringValue": f"record {i} {rnd.random()}"},
             "traceId": f"{i:032x}", "spanId": f"{i:016x}",
             "attributes": [{"key": "request.id", "value": {"stringValue": f"req-{i}"}},
                            {"key": "n", "value": {"intValue": str(i)}},
                            {"key": "ratio", "value": {"doubleValue": rnd.random()}}]} for i in range(N)]
    return {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": service}}, {"key": "host.name", "value": {"stringValue": "h1"}}]},
        "scopeLogs": [{"scope": {"name": "cmp"}, "logRecords": recs}]}]}


def endpoint():
    out = boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName="obs-phaseT2")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in out if o["OutputKey"] == "IngestEndpoint")


def send(label):
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    state.setdefault("base_ns", (int(time.time()) - 60) * 10**9)
    key = json.load(open(KEYS))[TENANT]["key"]
    body = gzip.compress(json.dumps(batch(f"cmp-{label}", state["base_ns"])).encode())
    req = urllib.request.Request(f"{endpoint()}/v1/logs", data=body, method="POST", headers={
        "x-api-key": key, "Content-Type": "application/json", "Content-Encoding": "gzip"})
    with urllib.request.urlopen(req) as r:
        print(f"sent {N} records as cmp-{label}: HTTP {r.status}")
    state[label] = time.time()
    json.dump(state, open(STATE, "w"))


def compare():
    state = json.load(open(STATE))
    lam = boto3.client("lambda", region_name=REGION, config=botocore.config.Config(read_timeout=900))
    t0 = state["base_ns"] // 10**9
    rows = {}
    for label in ("before", "after"):
        q = {"tenant": TENANT, "signal": "logs", "services": [f"cmp-{label}"],
             "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 - 60)),
             "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 120)), "search": {"limit": 5000}}
        out = json.loads(lam.invoke(FunctionName="obs-query", Payload=json.dumps(q).encode())["Payload"].read())
        cols = out["columns"]
        # Everything but the service name and observed time (set by the SDK clock; not sent here).
        keep = [c for c in cols if c not in ("service", "resource_attributes")]
        res = [{**{c: r[cols.index(c)] for c in keep},
                "resource": {k: v for k, v in r[cols.index("resource_attributes")].items() if k != "service.name"}}
               for r in out["rows"]]
        rows[label] = sorted(res, key=lambda r: r["ts_unix_nano"])
        print(f"cmp-{label}: {len(res)} records read back ({out['stats']['files']} files)")
    same = rows["before"] == rows["after"] and len(rows["before"]) == N
    diffs = [(a, b) for a, b in zip(rows["before"], rows["after"]) if a != b][:3]
    print("PASS  every field of every record identical before and after the switch" if same
          else f"FAIL  records differ: {len(rows['before'])} vs {len(rows['after'])}; e.g. {diffs}")
    sys.exit(0 if same else 1)


if __name__ == "__main__":
    if sys.argv[1:2] == ["send"] and sys.argv[2:3] in (["before"], ["after"]):
        send(sys.argv[2])
    elif sys.argv[1:2] == ["compare"]:
        compare()
    else:
        sys.exit(__doc__)
