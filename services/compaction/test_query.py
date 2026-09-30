"""Query engine against moto: real compaction, lookups, DuckDB and fan-out
(workers run in-process). The real-AWS version is infra/phase4-test.sh."""

import gzip
import json
from datetime import date, datetime, timedelta
import math
import os
import random
import time

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
    monkeypatch.setattr(query, "FILE_COST_BYTES", 0)
    files = [{"file_path": f"s3://b/{i}", "size_bytes": s, "kind": "parquet"} for i, s in enumerate([900, 500, 400, 300, 100])]
    chunks = query.plan_chunks(files, 8)
    sizes = sorted(sum(f["size_bytes"] for f in c) for c in chunks)
    assert len(chunks) == 3 and sizes == [600, 700, 900]      # ceil(2200 / 1000) workers, largest first
    assert len(query.plan_chunks(files, 2)) == 2               # capped by the caller's limit
    assert query.plan_chunks([], 4) == []


def test_reading_in_place_matches_downloading_and_reads_less(data):
    q = dict(where=[{"field": "severity_number", "op": ">=", "value": 17}],
             group_by=["attributes.http.route"], aggs=[{"fn": "count"}])
    ranged, downloaded = run(read="ranges", **q), run(read="download", **q)
    assert sorted(ranged["rows"]) == sorted(downloaded["rows"])
    assert ranged["stats"]["bytes"] <= downloaded["stats"]["bytes"]   # these test files are tiny


def test_range_reads_fetch_only_the_needed_columns(aws, tmp_path):  # noqa: F811
    import duckdb
    path = tmp_path / "wide.parquet"
    duckdb.sql(f"COPY (SELECT range AS id, repeat(md5(range::VARCHAR), 6) AS big, range % 7 AS sev "
               f"FROM range(500000)) TO '{path}' (FORMAT parquet)")
    data = path.read_bytes()
    boto3.client("s3").put_object(Bucket="obs-data-test", Key="data/tenant=acme/logs/wide.parquet", Body=data)
    fs = query.TenantS3(boto3.client("s3"), "obs-data-test", {"data/tenant=acme/logs/wide.parquet": len(data)})
    con = duckdb.connect()
    con.register_filesystem(fs)
    got = con.execute("SELECT sev, count(*) FROM read_parquet('obsq://data/tenant=acme/logs/wide.parquet') "
                      "GROUP BY 1 ORDER BY 1").fetchall()
    assert got[0] == (0, 71429)
    assert fs.bytes_read < len(data) / 10 and fs.requests < 20


def test_search_reading_in_place_has_only_the_files_columns(data):
    """S3 paths contain dt=/hour=/service= folders; they must not become columns."""
    ranged, downloaded = run(read="ranges", search={"limit": 5}), run(read="download", search={"limit": 5})
    assert ranged["columns"] == downloaded["columns"] and "dt" not in ranged["columns"]
    assert ranged["rows"] == downloaded["rows"]
    json.dumps(ranged)   # everything serialisable


# ---- HTTP API (POST /v1/query) ----

@pytest.fixture(autouse=True)
def today_is_day_after(monkeypatch):
    # The API keeps RETENTION_DAYS of data: pin "today" so the test day is always within it.
    monkeypatch.setattr(query, "_now", lambda: datetime.fromisoformat(f"{DAY}T12:00:00+00:00") + timedelta(days=1))


def http(tenant, body, b64=False):
    import base64 as b
    raw = body if isinstance(body, str) else json.dumps(body)
    event = {"requestContext": {"authorizer": {"tenant": tenant, "scope": "read"}} if tenant else {},
             "body": b.b64encode(raw.encode()).decode() if b64 else raw, "isBase64Encoded": b64}
    out = query.api(event, None)
    return out["statusCode"], json.loads(out["body"])


def test_api_answers_for_the_keys_tenant_only(data, monkeypatch):
    monkeypatch.setattr(query, "_invoke_worker", query.run_worker)
    q = {"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z", "aggs": [{"fn": "count"}]}
    status, out = http("acme", q)
    assert status == 200 and out["columns"] == ["count"] and "stats" in out
    status, out = http("acme", {**q, "tenant": "globex", "workers": 64, "read": "download"})
    assert status == 200 and out["rows"] == [[len(data)]]          # acme's rows; tenant in the body ignored
    assert http("globex", q)[1]["rows"] == [[50]]
    assert http("acme", q, b64=True)[1]["rows"] == [[len(data)]]   # API Gateway base64-encodes bodies


@pytest.mark.parametrize("tenant,body,status,msg", [
    (None, {"start": "x", "end": "y"}, 401, "no tenant"),
    ("acme", "not json", 400, "JSON"),
    ("acme", "[1, 2]", 400, "JSON object"),
    ("acme", {"signal": "logs"}, 400, "start and end"),
    ("acme", {"start": f"{DAY}T00:00:00Z", "end": f"{DAY}T01:00:00Z", "where": [{"field": "x;", "op": "="}]}, 400, "unknown field"),
    ("acme", {"start": "yesterday", "end": f"{DAY}T01:00:00Z"}, 400, "bad query"),
])
def test_api_rejects_bad_requests(data, tenant, body, status, msg):
    got, out = http(tenant, body)
    assert got == status and msg in out["error"]


def http_user(claims, body, resource="/v1/app/query"):
    event = {"resource": resource, "requestContext": {"authorizer": {"claims": claims}},
             "body": json.dumps(body), "isBase64Encoded": False}
    out = query.api(event, None)
    return out["statusCode"], json.loads(out["body"])


def test_signed_in_users_query_their_tenant_only(data, monkeypatch):
    monkeypatch.setattr(query, "_invoke_worker", query.run_worker)
    q = {"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z", "aggs": [{"fn": "count"}]}
    ana = {"custom:tenant": "acme", "email": "ana@example.com"}
    status, out = http_user(ana, {**q, "tenant": "globex"})
    assert status == 200 and out["rows"] == [[len(data)]]
    assert http_user({"custom:tenant": "globex"}, q)[1]["rows"] == [[50]]
    assert http_user(ana, None, resource="/v1/app/me") == (200, {"tenant": "acme", "email": "ana@example.com"})
    for bad in ({}, {"email": "x@example.com"}, {"custom:tenant": "Acme#x"}):
        assert http_user(bad, q)[0] == 401


def test_time_buckets_make_a_time_series(data, monkeypatch):
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1)   # several workers: buckets must merge
    out = run(group_by=["ts:3600", "service"], aggs=[{"fn": "count"}], limit=10000, workers=8)
    got = {(r[0], r[1]): r[2] for r in out["rows"]}
    want = {}
    for i, r in enumerate(data):
        hour = int(r["timeUnixNano"]) // 10**9 // 3600 * 3600
        svc = "web" if 150 <= i < 300 else "api"
        k = (time.strftime("%Y-%m-%dT%H:00:00.000000Z", time.gmtime(hour)), svc)
        want[k] = want.get(k, 0) + 1
    assert got == want
    with pytest.raises(query.BadQuery, match="time bucket"):
        query.compile_query({"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T01:00:00Z",
                             "group_by": ["ts:7"]})


# ------------------------------------------------------------------ counters (metrics "increase")

def metric_points(minute):
    """One minute of points every 10 s: a cumulative counter per route (/a restarts at 5:00), a delta
    counter, a gauge and a cumulative histogram. -> {(metric, route): [(ts_ns, value)]}, the OTLP doc."""
    pts, a_start = {}, 100
    for k in range(6):
        i = minute * 6 + k
        ts = H10 + i * 10 * 10**9
        a = a_start + 5 * i if i < 30 else 3 + 5 * (i - 30)        # /a restarts from zero at 10:05:00
        pts.setdefault(("reqs", "/a"), []).append((ts, a))
        pts.setdefault(("reqs", "/b"), []).append((ts, 2 * i))
        pts.setdefault(("jobs", None), []).append((ts, 4))
        pts.setdefault(("mem", None), []).append((ts, 1000 + i))
        pts.setdefault(("latency", None), []).append((ts, 10 * i))  # histogram count
    def dp(ts, route=None, **kw):
        return {"timeUnixNano": str(ts), "startTimeUnixNano": str(H10),
                "attributes": [{"key": "route", "value": {"stringValue": route}}] if route else [], **kw}
    metrics = [
        {"name": "reqs", "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints":
            [dp(t, r, asInt=str(v)) for r in ("/a", "/b") for t, v in pts[("reqs", r)]]}},
        {"name": "jobs", "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints":
            [dp(t, asInt=str(v)) for t, v in pts[("jobs", None)]]}},
        {"name": "mem", "gauge": {"dataPoints": [dp(t, asDouble=v) for t, v in pts[("mem", None)]]}},
        {"name": "latency", "histogram": {"aggregationTemporality": 2, "dataPoints":
            [dp(t, count=str(v), sum=25.0 * v, bucketCounts=[str(v)], explicitBounds=[]) for t, v in pts[("latency", None)]]}},
    ]
    doc = {"resourceMetrics": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
                                "scopeMetrics": [{"scope": {"name": "m"}, "metrics": metrics}]}]}
    return pts, doc


@pytest.fixture
def counters(aws, monkeypatch):  # noqa: F811
    """10 minutes of metrics: minutes 0-4 compacted to Parquet, 5-9 in fast-lane files (one per minute)."""
    handler, lookup = aws
    series = {}
    for m in range(10):
        pts, doc = metric_points(m)
        for k, v in pts.items():
            series.setdefault(k, []).extend(v)
        key = f"_incoming/tenant=acme/metrics/dt={DAY}/hour=10/m{m}.json.gz"
        boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=gzip.compress(json.dumps(doc).encode()))
        if m == 4:
            hour = {"tenant": "acme", "signal": "metrics", "dt": DAY, "hour": "10"}
            for b in handler.dispatcher({"plan_only": hour}, ctx(0))["planned"]:
                handler.worker({**hour, "batch_id": b}, ctx(1))
        elif m > 4:
            handler.recent_indexer({"detail": {"object": {"key": key}}}, None)
    monkeypatch.setattr(query, "lookup", lookup)
    return series


def expected_rise(series, metric, route, bucket_s=60, cumulative=True):
    out, prev = {}, None
    for ts, v in series[(metric, route)]:
        rise = v if not cumulative else (None if prev is None else (v - prev if v >= prev else v))
        prev = v
        if rise is not None:
            b = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(ts // 10**9 // bucket_s * bucket_s))
            out[b] = out.get(b, 0) + rise
    return out


def test_counter_increase_per_minute_matches_any_number_of_workers(counters, monkeypatch):
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1)   # one worker per file: series cross workers
    q = dict(signal="metrics", where=[{"field": "metric_name", "op": "=", "value": "reqs"}],
             group_by=["ts:60", "attributes.route"], aggs=[{"fn": "increase", "field": "value"}], limit=10000)
    one, many = run(workers=1, **q), run(workers=8, **q)
    assert many["stats"]["workers"] > 1 and one["stats"]["workers"] == 1
    assert sorted(one["rows"]) == sorted(many["rows"])
    for route in ("/a", "/b"):
        got = {r[0]: r[2] for r in many["rows"] if r[1] == route}
        assert got == expected_rise(counters, "reqs", route)
    # /a restarted: the minute of the restart still counts its rise (3 + 5 * 5), not a negative jump
    assert {r[0]: r[2] for r in many["rows"] if r[1] == "/a"}["2026-09-26T10:05:00.000000Z"] == 3 + 5 * 5


def test_delta_counters_gauges_and_histograms(counters, monkeypatch):
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1)
    def per_minute(metric, field="value", fn="increase"):
        out = run(signal="metrics", workers=8, where=[{"field": "metric_name", "op": "=", "value": metric}],
                  group_by=["ts:60"], aggs=[{"fn": fn, "field": field}], limit=10000)
        return {r[0]: r[1] for r in out["rows"]}
    assert per_minute("jobs") == expected_rise(counters, "jobs", None, cumulative=False)   # 6 points x 4 a minute
    assert set(per_minute("mem").values()) == {None}                                       # a gauge has no increase
    assert per_minute("mem", fn="avg")["2026-09-26T10:01:00.000000Z"] == 1000 + 8.5        # points 6..11
    assert per_minute("latency", "count") == expected_rise(counters, "latency", None)
    assert per_minute("latency", "sum") == {b: 25.0 * v for b, v in expected_rise(counters, "latency", None).items()}
    with pytest.raises(query.BadQuery, match="increase"):
        query.compile_query({"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T01:00:00Z",
                             "aggs": [{"fn": "increase", "field": "value"}]})


def test_contiguous_chunks_follow_time_order(monkeypatch):
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1000)
    monkeypatch.setattr(query, "FILE_COST_BYTES", 0)
    files = [{"file_path": f"s3://b/{i}", "size_bytes": s, "min_ts": f"2026-09-26T10:0{i}:00Z"}
             for i, s in enumerate([900, 100, 500, 400, 300])]
    chunks = query.plan_chunks(files, 8, contiguous=True)
    assert [[f["file_path"][-1] for f in c] for c in chunks] == [["0"], ["1", "2"], ["3", "4"]]


def test_many_small_files_are_spread_over_workers(monkeypatch):
    """A week of a small tenant's metrics: 3,000 files of ~14 KB. By size alone that is one worker
    opening 3,000 files one after another (27 s measured); each file's open cost spreads them."""
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 64 * 2**20)
    at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1790380800 + s))   # noqa: E731
    files = [{"file_path": f"s3://b/{i}", "size_bytes": 14_000, "min_ts": at(i * 200), "max_ts": at(i * 200 + 199)}
             for i in range(3000)]
    for contiguous in (False, True):
        chunks = query.plan_chunks(files, 64, contiguous)
        sizes = [len(c) for c in chunks]
        assert len(chunks) == 48 and max(sizes) - min(sizes) <= 2 and sum(sizes) == 3000
    monkeypatch.setattr(query, "FILE_COST_BYTES", 0)
    assert len(query.plan_chunks(files, 64)) == 1                 # the old behaviour


def test_counter_increase_is_exact_when_files_overlap_in_time(aws, monkeypatch):  # noqa: F811
    """Raw files holding interleaved minutes (e.g. compaction batches of a backfill) give files whose
    time ranges overlap. Workers must never split inside an overlap, or a rise is counted twice."""
    handler, lookup = aws
    series = {}
    docs = {m: metric_points(m) for m in range(8)}
    for name, minutes in (("odd", [1, 3, 5, 7]), ("even", [0, 2, 4, 6])):
        lines = []
        for m in minutes:
            pts, doc = docs[m]
            for k, v in pts.items():
                series.setdefault(k, []).extend(v)
            lines.append(json.dumps(doc))
        key = f"_incoming/tenant=acme/metrics/dt={DAY}/hour=10/{name}.json.gz"
        boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=gzip.compress("\n".join(lines).encode()))
        handler.recent_indexer({"detail": {"object": {"key": key}}}, None)
    for k in series:
        series[k].sort()
    monkeypatch.setattr(query, "lookup", lookup)
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1)
    out = run(signal="metrics", workers=8, where=[{"field": "metric_name", "op": "=", "value": "reqs"}],
              group_by=["ts:60", "attributes.route"], aggs=[{"fn": "increase", "field": "value"}], limit=10000)
    for route in ("/a", "/b"):
        assert {r[0]: r[2] for r in out["rows"] if r[1] == route} == expected_rise(series, "reqs", route)


def test_contiguous_chunks_never_cut_inside_an_overlap(monkeypatch):
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1)
    monkeypatch.setattr(query, "FILE_COST_BYTES", 0)
    f = lambda i, a, b: {"file_path": f"s3://b/{i}", "size_bytes": 100,   # noqa: E731
                         "min_ts": f"{DAY}T10:{a:02d}:00Z", "max_ts": f"{DAY}T10:{b:02d}:00Z"}
    files = [f(0, 0, 59), f(1, 21, 44), f(2, 30, 50), f(3, 59, 59), f(4, 1, 5)]
    assert [[x["file_path"][-1] for x in c] for c in query.plan_chunks(files, 8, contiguous=True)] == [["0", "4", "1", "2", "3"]]
    files = [f(0, 0, 9), f(1, 5, 14), f(2, 15, 20), f(3, 21, 30), f(4, 25, 26)]
    assert [[x["file_path"][-1] for x in c] for c in query.plan_chunks(files, 8, contiguous=True)] == [["0", "1"], ["2"], ["3", "4"]]


def test_api_never_reaches_before_retention(data, monkeypatch):
    monkeypatch.setattr(query, "_invoke_worker", query.run_worker)
    q = {"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z", "aggs": [{"fn": "count"}]}
    assert http("acme", q)[1]["rows"] == [[len(data)]]
    monkeypatch.setattr(query, "RETENTION_DAYS", 0)                 # today only: the test day is gone
    assert query.kept_from().isoformat().startswith(str(date.fromisoformat(DAY) + timedelta(days=1)))
    status, out = http("acme", q)
    assert status == 200 and out["rows"] == []                      # nothing older is read
    status, out = http("acme", {**q, "end": f"{DAY}T23:59:59Z", "start": "2020-01-01T00:00:00Z"})
    assert status == 200 and out["rows"] == []                      # nothing older is read


def test_not_in_and_not_exists(data):
    q = dict(aggs=[{"fn": "count"}])
    total = run(**q)["rows"][0][0]
    b = run(**q, where=[{"field": "attributes.http.route", "op": "=", "value": "/b"}])["rows"][0][0]
    got = run(**q, where=[{"field": "attributes.http.route", "op": "not_in", "value": ["/b"]}])["rows"][0][0]
    assert got == total - b and 0 < b < total
    # Rows without the attribute are kept by not_in, and are exactly those not_exists finds.
    assert run(**q, where=[{"field": "attributes.nope", "op": "not_in", "value": ["x"]}])["rows"][0][0] == total
    assert run(**q, where=[{"field": "attributes.nope", "op": "not_exists"}])["rows"][0][0] == total
    assert run(**q, where=[{"field": "attributes.http.route", "op": "not_exists"}])["rows"][0][0] == 0
    # Placeholders stay aligned with other conditions around it.
    two = run(**q, where=[{"field": "attributes.http.route", "op": "not_in", "value": ["/a", "/c"]},
                          {"field": "severity_text", "op": "=", "value": "ERROR"}])["rows"][0][0]
    assert two == run(**q, where=[{"field": "attributes.http.route", "op": "=", "value": "/b"},
                                  {"field": "severity_text", "op": "=", "value": "ERROR"}])["rows"][0][0] > 0
    assert run(**q, where=[{"field": "attributes.http.route", "op": "not_in", "value": []}])["rows"][0][0] == total
