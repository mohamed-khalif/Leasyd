#!/usr/bin/env python3
"""Phases 4-5 query engine tests on AWS, over the T6 load-test data.

    python3 infra/phase4-test.py [--tenant t6-000]

Against the largest load-test tenant (20 services, ~19% of the load):
  1. "Count errors by endpoint, last 24 h" with one worker (the Phase 4
     baseline) and with fan-out (Phase 5); both must agree. Target: < 5 s.
  2. p95 latency by service over 24 h (fan-out).
  3. Search: the 50 newest log lines containing "timeout" in the last hour.
  4. ID lookup across 30 days for a trace id taken from (3). Target: < 3 s.
  5. The same error counts as Athena for one fully compacted hour.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
lam = boto3.client("lambda", region_name=REGION, config=botocore.config.Config(
    read_timeout=900, retries={"max_attempts": 0}))
athena = boto3.client("athena", region_name=REGION)
FAILED = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        FAILED.append(msg)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def query(q):
    t0 = time.time()
    r = lam.invoke(FunctionName="obs-query", Payload=json.dumps(q).encode())
    out = json.loads(r["Payload"].read())
    if r.get("FunctionError") or "error" in out:
        sys.exit(f"query failed: {out}")
    out["wall_s"] = time.time() - t0
    return out


def describe(out):
    s = out["stats"]
    return (f"{out['wall_s']:.2f}s wall ({s['total_ms']} ms in the engine: lookup {s['lookup_ms']} ms, scan "
            f"{s['scan_ms']} ms); {s['files']} files, {s['bytes'] / 1e6:.0f} MB, {s['workers']} workers")


def run_athena(sql):
    qid = athena.start_query_execution(QueryString=sql, WorkGroup="obs")["QueryExecutionId"]
    while True:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        if st not in ("QUEUED", "RUNNING"):
            break
        time.sleep(1)
    if st != "SUCCEEDED":
        sys.exit(f"Athena {st}")
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"][1:]
    return [[c.get("VarCharValue") for c in r["Data"]] for r in rows]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", default="t6-000")
    a = p.parse_args()
    now = datetime.now(timezone.utc)
    base = {"tenant": a.tenant, "signal": "logs"}
    day = dict(base, start=iso(now - timedelta(hours=24)), end=iso(now))

    # 1. errors by endpoint, 24 h: one worker, then fan-out
    errors = dict(day, where=[{"field": "severity_number", "op": ">=", "value": 17}],
                  group_by=["attributes.http.route"], aggs=[{"fn": "count"}])
    one = query(dict(errors, workers=1))
    print(f"INFO  one worker: {describe(one)}")
    many = query(errors)
    print(f"INFO  fan-out:    {describe(many)}")
    check(sorted(map(tuple, one["rows"])) == sorted(map(tuple, many["rows"])) and many["rows"],
          f"errors by endpoint: same {len(many['rows'])} groups with 1 and {many['stats']['workers']} workers "
          f"({sum(r[1] for r in many['rows'])} errors)")
    check(many["wall_s"] < 5, f"1 day of the largest tenant in {many['wall_s']:.2f}s (target < 5 s)")

    # 2. p95 latency by service
    lat = query(dict(day, group_by=["service"], aggs=[{"fn": "count"}, {"fn": "p95", "field": "attributes.duration_ms"}]))
    timed = [r for r in lat["rows"] if r[0] != "t6-probe"]   # the prober's records carry no duration
    check(len(timed) > 1 and all(r[2] is not None for r in timed),
          f"p95 latency by service: {len(timed)} services in {lat['wall_s']:.2f}s")

    # 3. search
    hour = dict(base, start=iso(now - timedelta(hours=26)), end=iso(now))
    found = query(dict(hour, where=[{"field": "body", "op": "contains", "value": "timeout"}], search={"limit": 50}))
    cols = found["columns"]
    check(len(found["rows"]) == 50 and all("timeout" in r[cols.index("body")] for r in found["rows"]),
          f"search: 50 newest 'timeout' lines in {found['wall_s']:.2f}s")

    # 4. ID lookup across 30 days
    trace = found["rows"][-1][cols.index("trace_id")]
    idq = query(dict(base, start=iso(now - timedelta(days=30)), end=iso(now), match={"trace_id": trace},
                     search={"limit": 10}))
    lk = idq["stats"]["lookup"]
    check([r[idq["columns"].index("trace_id")] for r in idq["rows"]] == [trace],
          f"trace id across 30 days: found it in {idq['wall_s']:.2f}s; {lk['in_time_range']} files in range, "
          f"{lk.get('days_checked', 0)} day / {lk.get('hours_checked', 0)} hour filters checked, "
          f"{lk['after_bloom']} files read")
    check(idq["wall_s"] < 3, f"ID lookup across 30 days in {idq['wall_s']:.2f}s (target < 3 s)")

    # 5. Athena agrees for one fully compacted hour
    h = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    for back in range(3, 30):   # newest hour at least 3 h old that has data
        h = (now - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        ath = run_athena(f"SELECT attributes['http.route'], count(*) FROM obs.logs WHERE tenant = '{a.tenant}' "
                         f"AND dt = '{h:%Y-%m-%d}' AND hour = '{h:%H}' AND severity_number >= 17 GROUP BY 1")
        if ath:
            break
    ours = query(dict(base, start=iso(h), end=h.strftime("%Y-%m-%dT%H:59:59.999999Z"),
                      where=[{"field": "severity_number", "op": ">=", "value": 17}],
                      group_by=["attributes.http.route"], aggs=[{"fn": "count"}]))
    check(sorted((r, int(c)) for r, c in ath) == sorted((r, c) for r, c in ours["rows"]) and ath,
          f"same error counts as Athena for {h:%Y-%m-%d %H}:00 ({sum(int(c) for _, c in ath)} errors)")

    print(f"\n{len(FAILED)} failed" if FAILED else "\nall passed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
