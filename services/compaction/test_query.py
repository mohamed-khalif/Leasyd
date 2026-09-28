"""Query engine against moto: real compaction, lookups, DuckDB and fan-out
(workers run in-process). The real-AWS version is infra/phase4-test.sh."""

import gzip
import json
import math
import os
import random

import boto3
import pytest

from test_fastlane import aws, ctx  # noqa: F401  (fixture)

os.environ.setdefault("QUERY_WORKER_FUNCTION", "obs-query-worker")
import query  # noqa: E402

H10 = 1790416800 * 10**9  # 2026-09-26T10:00:00Z
DAY = "2026-09-26"
ROUTES = ["/a", "/b", "/c"]


def records(n, seed, tenant_tag=""):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        sev = 17 if rnd.random() < 0.2 else 9
        out.append({"timeUnixNano": str(H10 + (seed * 100000 + i) * 10**7), "severityNumber": sev,
                    "severityText": "ERROR" if sev == 17 else "INFO",
                    "body": {"stringValue": f"{tenant_tag}request {'timeout' if i % 7 == 0 else 'ok'} {i}"},
                    "traceId": f"{seed:08x}{i:024x}",
                    "attributes": [{"key": "http.route", "value": {"stringValue": ROUTES[i % 3]}},
                                   {"key": "duration_ms", "value": {"intValue": str(rnd.randint(1, 1000))}}]})
    return out


def put(key, service, recs):
    doc = {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": service}}]}, "scopeLogs": [{"logRecords": recs}]}]}
    boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=gzip.compress(json.dumps(doc).encode()))


@pytest.fixture
def data(aws, monkeypatch):  # noqa: F811
    """acme: 300 records compacted to Parquet (api, web) + 100 in a raw fast-lane file (api).
    globex: 50 records, which acme must never see."""
    handler, lookup = aws
    comp, raw = records(150, 1) + records(150, 2), records(100, 3)
    put(f"_incoming/tenant=acme/logs/dt={DAY}/hour=10/a.json.gz", "api", comp[:150])
    put(f"_incoming/tenant=acme/logs/dt={DAY}/hour=10/b.json.gz", "web", comp[150:])
    hour = {"tenant": "acme", "signal": "logs", "dt": DAY, "hour": "10"}
    for b in handler.dispatcher({"plan_only": hour}, ctx(0))["planned"]:
        handler.worker({**hour, "batch_id": b}, ctx(1))
    key = f"_incoming/tenant=acme/logs/dt={DAY}/hour=11/c.json.gz"
    put(key, "api", raw)
    handler.recent_indexer({"detail": {"object": {"key": key}}}, None)
    gkey = f"_incoming/tenant=globex/logs/dt={DAY}/hour=10/g.json.gz"
    put(gkey, "api", records(50, 4, tenant_tag="GLOBEX "))
    handler.recent_indexer({"detail": {"object": {"key": gkey}}}, None)
    monkeypatch.setattr(query, "lookup", lookup)   # the reloaded module, bound to moto
    return comp + raw


def run(**kw):
    q = {"tenant": "acme", "signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z", **kw}
    return query.run(q, invoke_worker=query.run_worker)


def attr(r, k):
    return next(a["value"] for a in r["attributes"] if a["key"] == k)


def test_count_errors_by_route_over_parquet_and_raw(data):
    out = run(where=[{"field": "severity_number", "op": ">=", "value": 17}],
              group_by=["attributes.http.route"], aggs=[{"fn": "count"}])
    want = {}
    for r in data:
        if r["severityNumber"] >= 17:
            want[attr(r, "http.route")["stringValue"]] = want.get(attr(r, "http.route")["stringValue"], 0) + 1
    assert out["columns"] == ["attributes.http.route", "count"]
    assert dict((k, v) for k, v in out["rows"]) == want
    assert [r[1] for r in out["rows"]] == sorted(want.values(), reverse=True)   # ordered by count
    kinds = out["stats"]["lookup"]
    assert out["stats"]["files"] >= 3 and kinds["in_time_range"] >= 3


def test_numeric_aggregates_and_percentiles(data):
    out = run(aggs=[{"fn": "count"}, {"fn": "sum", "field": "attributes.duration_ms"},
                    {"fn": "min", "field": "attributes.duration_ms"}, {"fn": "max", "field": "attributes.duration_ms"},
                    {"fn": "avg", "field": "attributes.duration_ms"}, {"fn": "p95", "field": "attributes.duration_ms"}])
    d = sorted(int(attr(r, "duration_ms")["intValue"]) for r in data)
    [[count, total, lo, hi, avg, p95]] = out["rows"]
    assert (count, total, lo, hi) == (len(d), sum(d), min(d), max(d))
    assert math.isclose(avg, sum(d) / len(d))
    exact = d[math.ceil(0.95 * len(d)) - 1]
    assert abs(p95 - exact) / exact < 0.03


def test_search_newest_matching_rows(data):
    out = run(where=[{"field": "body", "op": "contains", "value": "TIMEOUT"}], search={"limit": 5})
    want = sorted((r for r in data if "timeout" in r["body"]["stringValue"]), key=lambda r: -int(r["timeUnixNano"]))[:5]
    ts = out["columns"].index("ts_unix_nano")
    assert [row[ts] for row in out["rows"]] == [int(r["timeUnixNano"]) for r in want]
    body = out["columns"].index("body")
    assert all("timeout" in row[body] for row in out["rows"])


def test_trace_id_match_returns_exactly_that_record(data):
    t = data[42]["traceId"]
    out = run(match={"trace_id": t.upper()}, search={"limit": 10})
    tid = out["columns"].index("trace_id")
    assert [row[tid] for row in out["rows"]] == [t]


def test_fan_out_gives_the_same_answer(data, monkeypatch):
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1)   # one worker per file
    q = dict(group_by=["service"], aggs=[{"fn": "count"}, {"fn": "p50", "field": "attributes.duration_ms"},
                                         {"fn": "avg", "field": "attributes.duration_ms"}])
    one = run(workers=1, **q)
    many = run(workers=8, **q)
    assert many["stats"]["workers"] > 1 and one["stats"]["workers"] == 1
    assert sorted(one["rows"]) == sorted(many["rows"])


def test_tenant_sees_only_its_own_data(data):
    out = run(where=[{"field": "body", "op": "contains", "value": "GLOBEX"}], aggs=[{"fn": "count"}])
    assert out["rows"] == [[0]]
    g = query.run({"tenant": "globex", "signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z",
                   "aggs": [{"fn": "count"}]}, invoke_worker=query.run_worker)
    assert g["rows"] == [[50]]


@pytest.mark.parametrize("bad", [
    {"group_by": ["body; DROP TABLE t"]},
    {"where": [{"field": "severity_number", "op": "LIKE", "value": 1}]},
    {"where": [{"field": "attributes.x']) OR 1=1 --", "op": "=", "value": 1}]},
    {"aggs": [{"fn": "exec", "field": "body"}]},
    {"signal": "profiles"},
])
def test_bad_queries_rejected(bad):
    q = {"tenant": "acme", "signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z", **bad}
    assert "error" in query.handler(q, None)


def test_plan_chunks_balances_by_size(monkeypatch):
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1000)
    files = [{"file_path": f"s3://b/{i}", "size_bytes": s, "kind": "parquet"} for i, s in enumerate([900, 500, 400, 300, 100])]
    chunks = query.plan_chunks(files, 8)
    sizes = sorted(sum(f["size_bytes"] for f in c) for c in chunks)
    assert len(chunks) == 3 and sizes == [600, 700, 900]      # ceil(2200 / 1000) workers, largest first
    assert len(query.plan_chunks(files, 2)) == 2               # capped by the caller's limit
    assert query.plan_chunks([], 4) == []
