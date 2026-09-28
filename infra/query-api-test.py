#!/usr/bin/env python3
"""End-to-end test of the query API (POST /v1/query) on AWS.

    python3 infra/query-api-test.py [--tenant t6-000] [--other t6-001]

Issues a read key for --tenant (revoked at the end) and checks:
  - the API answers a query with the same rows as the query engine itself;
  - a tenant named in the body is ignored (the key decides the tenant);
  - the read key cannot send data, and an ingest key cannot query;
  - no key -> 401, a bad query -> 400.
Ingest keys come from the load-test keys file (~/.obs-t6-keys.json).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

FAILED = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        FAILED.append(msg)


def post(endpoint, path, key, body):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-api-key"] = key
    req = urllib.request.Request(endpoint + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read() or b"{}"), time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}"), time.time() - t0
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}, time.time() - t0


def post_ok(endpoint, path, key, body):
    """post() for calls that should succeed: a new key is refused (403) by
    some API Gateway nodes for a while after it starts working on others."""
    for _ in range(10):
        status, out, secs = post(endpoint, path, key, body)
        if status != 403:
            break
        time.sleep(3)
    return status, out, secs


def admin(lam, payload):
    out = json.loads(lam.invoke(FunctionName="obs-tenant-admin", Payload=json.dumps(payload).encode())["Payload"].read())
    if "error" in out:
        sys.exit(f"tenant admin: {out['error']}")
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", default="t6-000")
    p.add_argument("--other", default="t6-001")
    p.add_argument("--keys", default=os.path.expanduser("~/.obs-t6-keys.json"))
    a = p.parse_args()
    lam = boto3.client("lambda", config=__import__("botocore.config").config.Config(read_timeout=300))
    endpoint = boto3.client("cloudformation").describe_stacks(StackName="obs-phaseT2")["Stacks"][0]["Outputs"]
    endpoint = next(o["OutputValue"] for o in endpoint if o["OutputKey"] == "IngestEndpoint")
    ingest_key = json.load(open(a.keys))[a.tenant]["key"]

    rk = admin(lam, {"action": "read-key", "tenant": a.tenant})
    key = rk["api_key"]
    try:
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
        q = {"signal": "logs", "start": iso(now - timedelta(hours=24)), "end": iso(now),
             "group_by": ["service"], "aggs": [{"fn": "count"}]}

        # New keys take 1-6 minutes to reach every API Gateway node (6 measured on 2026-09-28).
        ok_in_a_row, deadline = 0, time.time() + 600
        while ok_in_a_row < 5 and time.time() < deadline:
            status, _, _ = post(endpoint, "/v1/query", key, {**q, "start": iso(now - timedelta(minutes=1))})
            ok_in_a_row = ok_in_a_row + 1 if status == 200 else 0
            time.sleep(2 if status == 200 else 5)
        check(ok_in_a_row >= 5, f"read key active for {a.tenant}")

        status, out, secs = post_ok(endpoint, "/v1/query", key, q)
        direct = json.loads(lam.invoke(FunctionName="obs-query",
                                       Payload=json.dumps({**q, "tenant": a.tenant}).encode())["Payload"].read())
        check(status == 200 and sorted(out.get("rows", [])) == sorted(direct["rows"]) and out["rows"],
              f"POST /v1/query: {len(out.get('rows', []))} services, same counts as the engine, {secs:.2f}s")

        other = json.loads(lam.invoke(FunctionName="obs-query",
                                      Payload=json.dumps({**q, "tenant": a.other}).encode())["Payload"].read())
        status, out2, _ = post_ok(endpoint, "/v1/query", key, {**q, "tenant": a.other})
        check(status == 200 and sorted(out2.get("rows", [])) == sorted(direct["rows"]) != sorted(other["rows"]),
              f"a tenant named in the body ({a.other}) is ignored: still {a.tenant}'s data (HTTP {status})")

        status, body, _ = post(endpoint, "/v1/logs", key, {"resourceLogs": []})
        check(status == 403, f"read key cannot send data (POST /v1/logs -> {status})")
        status, body, _ = post(endpoint, "/v1/query", ingest_key, q)
        check(status == 403, f"ingest key cannot query (POST /v1/query -> {status})")
        status, body, _ = post(endpoint, "/v1/query", None, q)
        check(status == 401, f"no key -> {status}")
        status, body, _ = post_ok(endpoint, "/v1/query", key, {**q, "group_by": ["body; DROP TABLE t"]})
        check(status == 400 and "unknown field" in body.get("error", ""), f"bad query -> {status}: {body.get('error')}")
        status, body, _ = post_ok(endpoint, "/v1/query", key, {"signal": "logs"})
        check(status == 400, f"missing time range -> {status}")

        trace_q = {**q, "search": {"limit": 5}}
        trace_q.pop("group_by"), trace_q.pop("aggs")
        status, rows, _ = post_ok(endpoint, "/v1/query", key, trace_q)
        tid = rows["columns"].index("trace_id") if status == 200 else None
        if tid is not None and rows["rows"]:
            trace = rows["rows"][0][tid]
            status, found, secs = post_ok(endpoint, "/v1/query", key, {
                "signal": "logs", "start": iso(now - timedelta(days=30)), "end": iso(now),
                "match": {"trace_id": trace}, "search": {"limit": 10}})
            check(status == 200 and trace in [r[found["columns"].index("trace_id")] for r in found["rows"]],
                  f"trace id across 30 days through the API: {secs:.2f}s")
        else:
            check(False, f"search through the API returned {status}")
    finally:
        admin(lam, {"action": "revoke", "tenant": a.tenant, "key_id": rk["key_id"]})
        print(f"read key {rk['key_id']} revoked")
    print(f"{len(FAILED)} failed" if FAILED else "all passed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
