"""Query engine against moto: real compaction, lookups, DuckDB and fan-out
(workers run in-process). The real-AWS version is infra/phase4-test.sh."""

import gzip
import json
from datetime import date, datetime, timedelta, timezone
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
        pts.setdefault(("latency", None), []).append((ts, 10 * i if i < 30 else 10 * (i - 30) + 5))  # histogram count; restarts
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
            [dp(t, count=str(v), sum=25.0 * v, bucketCounts=[str(c) for c in split(v)], explicitBounds=[10.0, 100.0])
             for t, v in pts[("latency", None)]]}},
    ]
    doc = {"resourceMetrics": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
                                "scopeMetrics": [{"scope": {"name": "m"}, "metrics": metrics}]}]}
    return pts, doc


def split(v):
    """The "latency" histogram's buckets (<= 10, <= 100, more) for a count of v."""
    return [v // 2, v // 4, v - v // 2 - v // 4]


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


def test_log_buckets_hashes_and_collapse(data):
    out = run(group_by=["log:severity_number"], aggs=[{"fn": "count"}])
    got = {r[0]: r[1] for r in out["rows"]}
    want = {}
    for r in data:   # 9 (INFO) -> 2*log2(9) = 6.3 -> 6; 17 (ERROR) -> 8.2 -> 8
        b = math.floor(math.log2(r["severityNumber"]) * query.LOG_STEPS)
        want[b] = want.get(b, 0) + 1
    assert got == want and set(got) == {6, 8}
    # Distinct attribute sets per service, as one row each ("groups"), counts summed.
    out = run(group_by=["service", "hash:attributes"], aggs=[{"fn": "count"}], collapse=1, limit=10)
    assert out["columns"] == ["service", "groups", "count"]
    got = {r[0]: (r[1], r[2]) for r in out["rows"]}
    sets, counts = {}, {}
    for i, r in enumerate(data):
        svc = "web" if 150 <= i < 300 else "api"
        sets.setdefault(svc, set()).add((attr(r, "http.route")["stringValue"], attr(r, "duration_ms")["intValue"]))
        counts[svc] = counts.get(svc, 0) + 1
    assert got == {s: (len(sets[s]), counts[s]) for s in sets} and "truncated" not in out


def test_series_per_metric(counters):
    q = {"tenant": "acme", "signal": "metrics", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z",
         "group_by": ["metric_name", "hash:attributes"], "aggs": [{"fn": "count"}], "collapse": 1}
    out = query.run(q, invoke_worker=query.run_worker)
    got = {r[0]: (r[1], r[2]) for r in out["rows"]}
    assert got == {"reqs": (2, 120), "jobs": (1, 60), "mem": (1, 60), "latency": (1, 60)}
    got = {r[1]: r[2] for r in query.run({**q, "group_by": ["metric_name", "log:value"], "collapse": None},
                                          invoke_worker=query.run_worker)["rows"] if r[0] == "mem"}
    assert got == {19: 24, 20: 36}   # mem is 1000..1059: 1024 starts bucket 20


@pytest.mark.parametrize("q, msg", [
    ({"group_by": ["log:body"]}, "log buckets"),
    ({"group_by": ["hash:body"]}, "hash is for"),
])
def test_bad_derived_groups(q, msg):
    with pytest.raises(query.BadQuery, match=msg):
        query.compile_query({"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T01:00:00Z", **q})


@pytest.mark.parametrize("q", [
    {"group_by": ["service"], "aggs": [{"fn": "count"}], "collapse": 1},
    {"group_by": ["service", "hash:attributes"], "aggs": [{"fn": "max", "field": "severity_number"}], "collapse": 1},
])
def test_bad_collapse(data, q):
    with pytest.raises(query.BadQuery, match="collapse"):
        run(**q)


def test_too_many_groups_is_reported(data, monkeypatch):
    monkeypatch.setattr(query, "MAX_ROWS", 5)
    out = run(group_by=["service", "hash:attributes"], aggs=[{"fn": "count"}], collapse=1)
    assert out["truncated"] is True


def test_promql_building_blocks(data, counters):
    # label.<key>: an attribute (or resource attribute); a|b tries each key
    by_route = {r[0]: r[1] for r in run(group_by=["label.nope|http.route"], aggs=[{"fn": "count"}])["rows"]}
    assert by_route == {r[0]: r[1] for r in run(group_by=["attributes.http.route"], aggs=[{"fn": "count"}])["rows"]}
    # regex: the whole value must match; a missing label counts as ""
    q = dict(aggs=[{"fn": "count"}])
    n = lambda **w: run(**q, where=[w])["rows"][0][0]
    assert n(field="label.http.route", op="regex", value="/(a|b)") == n(field="attributes.http.route", op="in", value=["/a", "/b"])
    assert n(field="label.http.route", op="regex", value="/a") + n(field="label.http.route", op="not_regex", value="/a") == run(**q)["rows"][0][0]
    assert n(field="label.nope", op="regex", value="") == run(**q)["rows"][0][0]
    with pytest.raises(query.BadQuery, match="bad regex"):
        run(**q, where=[{"field": "body", "op": "regex", "value": "("}])
    # any percentile, and the raw histogram behind it
    p999 = run(aggs=[{"fn": "p99.9", "field": "attributes.duration_ms"}, {"fn": "hist", "field": "attributes.duration_ms"}])["rows"][0]
    assert 900 < p999[0] <= 1050 and sum(p999[1].values()) == len(data)
    assert query._percentile({int(k): v for k, v in p999[1].items()}, 0.999) == p999[0]
    # last: the latest point's value, across workers
    mq = {"tenant": "acme", "signal": "metrics", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z",
          "where": [{"field": "metric_name", "op": "=", "value": "mem"}], "aggs": [{"fn": "last", "field": "value"}]}
    assert query.run(mq, invoke_worker=query.run_worker)["rows"][0][0] == counters[("mem", None)][-1][1]
    # series keys and their labels
    mq.update(where=[{"field": "metric_name", "op": "=", "value": "reqs"}], aggs=[{"fn": "count"}],
              group_by=["hash:series", "json:attributes"])
    rows = query.run(mq, invoke_worker=query.run_worker)["rows"]
    assert sorted(json.loads(r[1])["route"] for r in rows) == ["/a", "/b"] and len({r[0] for r in rows}) == 2
    # PromQL's own queries may return more groups than the API allows, up to a cap
    big = run(group_by=["span_id"], aggs=[{"fn": "count"}], limit=100000, max_rows=100000)
    assert len(big["rows"]) > query.MAX_ROWS or len(data) <= query.MAX_ROWS
    assert query._row_cap({"max_rows": 10**9}) == query.MAX_INTERNAL_ROWS


def test_a_crashed_worker_is_run_once_more(monkeypatch):
    import io
    calls = []

    class Lam:
        def invoke(self, **kw):
            calls.append(1)
            if len(calls) == 1:
                return {"FunctionError": "Unhandled", "Payload": io.BytesIO(b'{"errorType": "Runtime.ExitError"}')}
            return {"Payload": io.BytesIO(b'{"rows": []}')}
    monkeypatch.setattr(query, "lam", Lam())
    assert query._invoke_worker({}) == {"rows": []} and len(calls) == 2
    calls.clear()

    class Bad(Lam):
        def invoke(self, **kw):
            calls.append(1)
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(b'{"errorType": "ValueError"}')}
    monkeypatch.setattr(query, "lam", Bad())
    with pytest.raises(RuntimeError, match="ValueError"):
        query._invoke_worker({})
    assert len(calls) == 1          # a real error in the query is not retried


def test_histogram_buckets_per_minute_any_number_of_workers(counters, monkeypatch):
    """buckets: measurements per histogram bucket, the rise of each cumulative bucket (a restart
    counts whole), stitched across workers like increase."""
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1)
    q = dict(signal="metrics", where=[{"field": "metric_name", "op": "=", "value": "latency"}],
             group_by=["ts:60"], aggs=[{"fn": "buckets"}], limit=10000)
    one, many = run(workers=1, **q), run(workers=8, **q)
    assert many["stats"]["workers"] > 1 and sorted(one["rows"]) == sorted(many["rows"])
    expected, prev = {}, None
    for ts, v in counters[("latency", None)]:
        if prev is not None:
            rise = [c - p for c, p in zip(split(v), split(prev))] if v >= prev else split(v)
            b = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(ts // 10**9 // 60 * 60))
            acc = expected.setdefault(b, [0, 0, 0])
            expected[b] = [a + r for a, r in zip(acc, rise)]
        prev = v
    got = {r[0]: [r[1]["10.0"], r[1]["100.0"], r[1]["+Inf"]] for r in many["rows"]}
    assert got == expected and list(many["rows"][0][1]) == ["10.0", "100.0", "+Inf"]   # in bound order
    total = run(signal="metrics", workers=8, where=q["where"], aggs=[{"fn": "buckets"}])["rows"][0][0]
    assert sum(total.values()) == sum(sum(v) for v in expected.values())
    with pytest.raises(query.BadQuery, match="only aggregate"):
        query.compile_query({**q, "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T01:00:00Z", "aggs": [{"fn": "buckets"}, {"fn": "count"}]})


# ------------------------------------------------------------------ long queries (jobs)

def get_job(tenant, job):
    out = query.api({"httpMethod": "GET", "resource": "/v1/app/query/{job}", "pathParameters": {"job": job},
                     "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant}}}}, None)
    return out["statusCode"], json.loads(out["body"])


def test_long_queries_run_on_as_jobs(data, monkeypatch):
    """async: true, or a query past the deadline -> 202 and a job; GET answers 202 while it runs,
    then the query's own answer, to its tenant only."""
    monkeypatch.setattr(query, "_invoke_worker", lambda e: query.worker(e, None))
    started = []
    monkeypatch.setattr(query, "_run_in_background", started.append)
    q = {"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z", "aggs": [{"fn": "count"}]}
    ana = {"custom:tenant": "acme"}
    status, out = http_user(ana, {**q, "async": True})
    assert status == 202 and out["status"] == "running" and started[-1]["job"]["query"] == q
    job = out["job"]
    assert get_job("acme", job)[0] == 202
    assert get_job("globex", job)[0] == 404 and get_job("acme", "../x")[0] == 404 and get_job("acme", "0" * 32)[0] == 404
    query.worker(started[-1], None)                       # the background run (obs-query-worker)
    assert get_job("acme", job) == (200, {**get_job("acme", job)[1], "rows": [[len(data)]]})
    assert get_job("globex", job)[0] == 404
    # Past the deadline: the same query continues as a job, SQL and PromQL alike.
    real = query.answer
    monkeypatch.setattr(query, "SYNC_DEADLINE_S", 0.05)
    monkeypatch.setattr(query, "answer", lambda t, q: (time.sleep(0.5), real(t, q))[1])
    sql = {"sql": "SELECT count(*) FROM logs", "start": q["start"], "end": q["end"]}
    status, out = http_user(ana, sql)
    assert status == 202 and started[-1]["job"]["query"] == sql
    query.worker(started[-1], None)
    assert get_job("acme", out["job"])[1]["rows"] == [[len(data)]]
    # A job that never finished (the worker died) is reported, not left "running" forever.
    status, out = http_user(ana, {**q, "async": True})
    monkeypatch.setattr(query, "_now", lambda: datetime.now(timezone.utc) + timedelta(minutes=10))
    assert get_job("acme", out["job"])[0] == 504


def test_failed_jobs_say_so(data, monkeypatch):
    started = []
    monkeypatch.setattr(query, "_run_in_background", started.append)
    monkeypatch.setattr(query, "answer", lambda t, q: 1 / 0)
    status, out = http_user({"custom:tenant": "acme"}, {"signal": "logs", "async": True})
    with pytest.raises(ZeroDivisionError):
        query.worker(started[-1], None)                   # still a Lambda error (alarmed)
    assert get_job("acme", out["job"]) == (500, {"error": "the query failed; try a shorter time range"})


def test_queries_and_jobs_are_limited_per_tenant(data, monkeypatch):
    """Per tenant and minute, by plan; another tenant is unaffected; jobs have a lower limit."""
    boto3.client("dynamodb").create_table(
        TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
    boto3.resource("dynamodb").Table("obs-tenants").put_item(Item={"pk": "tenant#acme", "plan": "free"})
    query._plans.clear()
    monkeypatch.setitem(query.RATE_LIMITS, "query", {"free": 3, "standard": 5})
    monkeypatch.setitem(query.DAILY_SEARCH_UNITS, "free", 0)
    monkeypatch.setitem(query.RATE_LIMITS, "job", {"free": 1, "standard": 2})
    monkeypatch.setattr(query, "_run_in_background", lambda p: None)
    q = {"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z", "aggs": [{"fn": "count"}]}
    ana, bo = {"custom:tenant": "acme"}, {"custom:tenant": "globex"}
    assert [http_user(ana, q)[0] for _ in range(4)] == [200, 200, 200, 429]
    assert "at most 3 a minute" in http_user(ana, q)[1]["error"]
    assert [http_user(bo, q)[0] for _ in range(2)] == [200] * 2          # no plan item: standard (5, async ones included)
    assert http_user(bo, {**q, "async": True})[0] == 202
    assert http_user(bo, {**q, "async": True})[0] == 202
    status, out = http_user(bo, {**q, "async": True})
    assert status == 429 and "long-running" in out["error"]
    # A counting problem (e.g. no permission yet) never stops queries.
    query._plans.clear()
    boto3.client("dynamodb").delete_table(TableName="obs-tenants")
    assert http_user({"custom:tenant": "initech"}, q)[0] == 200


def test_daily_search_allowance(data, monkeypatch):
    """Each query uses search units (one per worker); past today's allowance: 429 until midnight."""
    boto3.client("dynamodb").create_table(
        TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
    t = boto3.resource("dynamodb").Table("obs-tenants")
    t.put_item(Item={"pk": "tenant#acme", "plan": "standard", "search_units_per_day": 3})
    t.put_item(Item={"pk": "tenant#globex", "plan": "standard", "search_units_per_day": 0})   # no daily limit
    query._plans.clear()
    q = {"signal": "logs", "start": f"{DAY}T00:00:00Z", "end": f"{DAY}T23:59:59Z", "aggs": [{"fn": "count"}]}
    ana = {"custom:tenant": "acme"}
    assert [http_user(ana, q)[0] for _ in range(3)] == [200, 200, 200]
    used = t.get_item(Key={"pk": f"usage#search#acme#{query._day()}"})["Item"]
    assert used["n"] == 3 and used["expires"] > time.time()
    status, out = http_user(ana, q)
    assert status == 429 and "allowance is used up (3 search units)" in out["error"]
    assert [http_user({"custom:tenant": "globex"}, q)[0] for _ in range(5)] == [200] * 5
    # A query with nothing to read (a time range without data) uses no units.
    empty = {**q, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}
    t.put_item(Item={"pk": "tenant#hooli", "plan": "standard", "search_units_per_day": 1})
    assert [http_user({"custom:tenant": "hooli"}, empty)[0] for _ in range(3)] == [200] * 3
    assert "Item" not in t.get_item(Key={"pk": f"usage#search#hooli#{query._day()}"})
    # SQL uses units by the data it reads: at least 1 when there is some, none when there isn't.
    monkeypatch.setattr(query, "_invoke_worker", lambda e: query.worker(e, None))
    sql = {"sql": "SELECT count(*) FROM logs", "start": q["start"], "end": q["end"]}
    assert http_user({"custom:tenant": "globex"}, sql)[0] == 200
    assert t.get_item(Key={"pk": f"usage#search#globex#{query._day()}"})["Item"]["n"] >= 6   # 5 queries + the SQL
    t.put_item(Item={"pk": "tenant#initech", "plan": "standard"})
    assert http_user({"custom:tenant": "initech"}, sql)[0] == 200
    assert "Item" not in t.get_item(Key={"pk": f"usage#search#initech#{query._day()}"})


def test_worker_out_of_file_descriptors_exits_for_a_fresh_container(monkeypatch):
    import errno as _errno
    exits = []
    monkeypatch.setattr(query.os, "_exit", lambda code: exits.append(code))

    def boom(event):
        try:
            raise OSError(_errno.EMFILE, "Too many open files")
        except OSError as inner:
            raise RuntimeError("SSLError: SSL validation failed") from inner
    monkeypatch.setattr(query, "run_worker", boom)
    with pytest.raises(RuntimeError):     # os._exit is patched to return here
        query.worker({"query": {}}, None)
    assert exits == [1]

    monkeypatch.setattr(query, "run_worker", lambda event: (_ for _ in ()).throw(ValueError("bad query")))
    with pytest.raises(ValueError):
        query.worker({"query": {}}, None)
    assert exits == [1]
