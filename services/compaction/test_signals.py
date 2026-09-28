"""Traces and metrics compaction (T4): spans and metric data points to
Parquet, with the same grouping, splitting and bloom rules as logs."""

import gzip
import json

import duckdb

import bloom
import compact

H20 = 1790452800 * 10**9  # 2026-09-26T20:00:00Z in ns
MS = 10**6


def attr(k, v):
    return {"key": k, "value": {"stringValue": v}}


def write(path, doc):
    with gzip.open(path, "wt") as f:
        f.write(json.dumps(doc) + "\n")
    return str(path)


def spans_doc(service, spans):
    return {"resourceSpans": [{"resource": {"attributes": [attr("service.name", service), attr("obs.x", "y")]},
                               "scopeSpans": [{"scope": {"name": "lib"}, "spans": spans}]}]}


def metrics_doc(service, metrics):
    return {"resourceMetrics": [{"resource": {"attributes": [attr("service.name", service)]},
                                 "scopeMetrics": [{"scope": {"name": "meter"}, "metrics": metrics}]}]}


def span(start, end=None, trace="ab" * 16, **kw):
    s = {"traceId": trace, "spanId": "cd" * 8, "name": "GET /x", "kind": 2,
         "startTimeUnixNano": str(start), "endTimeUnixNano": str(end or start + 5 * MS)}
    s.update(kw)
    return s


def rows(path, sql="SELECT * FROM t"):
    con = duckdb.connect()
    con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{path}')")
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


# ------------------------------------------------------------------ traces

def test_spans_flattened(tmp_path):
    f = write(tmp_path / "a.json.gz", spans_doc("api", [span(
        H20 + 7 * MS, H20 + 19 * MS, parentSpanId="ef" * 8, traceState="k=v",
        attributes=[attr("http.route", "/x"), {"key": "http.status_code", "value": {"intValue": "500"}}],
        status={"code": 2, "message": "boom"},
        events=[{"timeUnixNano": str(H20 + 8 * MS), "name": "exception",
                 "attributes": [attr("exception.type", "ValueError")]}],
        links=[{"traceId": "12" * 16, "spanId": "34" * 8, "attributes": [attr("l", "1")]}],
    )]))
    [w] = compact.compact("traces", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    assert (w["service"], w["hour"], w["rows"]) == ("api", "20", 1)
    assert w["relpath"] == "dt=2026-09-26/hour=20/service=api/part-b1-000.parquet"
    [r] = rows(w["path"])
    assert r["ts_unix_nano"] == H20 + 7 * MS and r["duration_ns"] == 12 * MS
    assert (r["name"], r["kind"], r["status_code"], r["status_message"]) == ("GET /x", 2, 2, "boom")
    assert (r["trace_id"], r["span_id"], r["parent_span_id"], r["trace_state"]) == (
        "ab" * 16, "cd" * 8, "ef" * 8, "k=v")
    assert r["attributes"] == {"http.route": "/x", "http.status_code": "500"}
    assert r["resource_attributes"] == {"service.name": "api"}  # obs.* dropped
    assert r["scope_name"] == "lib"
    [ev] = r["events"]
    assert (ev["name"], ev["attributes"]) == ("exception", {"exception.type": "ValueError"})
    assert str(ev["ts"]) == "2026-09-26 20:00:00.008000"
    [lk] = r["links"]
    assert (lk["trace_id"], lk["span_id"], lk["attributes"]) == ("12" * 16, "34" * 8, {"l": "1"})


def test_span_enum_names_accepted(tmp_path):
    f = write(tmp_path / "a.json.gz", spans_doc("api", [
        span(H20, kind="SPAN_KIND_CLIENT", status={"code": "STATUS_CODE_OK"}), span(H20 + 1)]))
    [w] = compact.compact("traces", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    got = rows(w["path"], "SELECT kind, status_code FROM t ORDER BY ts_unix_nano")
    assert got == [{"kind": 3, "status_code": 1}, {"kind": 2, "status_code": 0}]


def test_spans_split_by_start_hour_and_sorted(tmp_path):
    starts = [H20 + 3600 * 10**9 + 1, H20 + 9 * MS, H20 + 2 * MS]
    f = write(tmp_path / "a.json.gz", spans_doc("api", [span(s) for s in starts]))
    written = compact.compact("traces", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    assert [(w["hour"], w["rows"]) for w in written] == [("20", 2), ("21", 1)]
    assert [r["ts_unix_nano"] for r in rows(written[0]["path"], "SELECT ts_unix_nano FROM t")] == [
        H20 + 2 * MS, H20 + 9 * MS]


def test_span_bloom_holds_trace_ids_and_id_attributes(tmp_path):
    spans = [span(H20 + n, trace=f"{n:032X}", attributes=[attr("request.id", f"R{n}")]) for n in range(20)]
    f = write(tmp_path / "a.json.gz", spans_doc("api", spans))
    [w] = compact.compact("traces", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    assert w["bloom"].n == 40
    assert all(w["bloom"].might_contain(bloom.term("trace_id", f"{n:032x}")) for n in range(20))
    assert w["bloom"].might_contain(bloom.term("request.id", "r5"))
    assert not w["bloom"].might_contain(bloom.term("trace_id", "f" * 32))


def test_span_without_start_falls_back_to_arrival_hour(tmp_path):
    f = write(tmp_path / "a.json.gz", spans_doc("api", [{"traceId": "ab" * 16, "spanId": "cd" * 8, "name": "x"}]))
    [w] = compact.compact("traces", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    [r] = rows(w["path"])
    assert r["ts_unix_nano"] == H20 and r["duration_ns"] is None and r["kind"] is None


# ----------------------------------------------------------------- metrics

def pt(ts, **kw):
    return {"timeUnixNano": str(ts), "startTimeUnixNano": str(H20), "attributes": [attr("host", "h1")], **kw}


ALL_TYPES = [
    {"name": "cpu", "unit": "1", "gauge": {"dataPoints": [pt(H20 + 1 * MS, asDouble=0.5)]}},
    {"name": "reqs", "unit": "{req}", "description": "requests",
     "sum": {"aggregationTemporality": 2, "isMonotonic": True,
             "dataPoints": [pt(H20 + 2 * MS, asInt="41"), pt(H20 + 3 * MS, asInt="42")]}},
    {"name": "latency", "unit": "ms", "histogram": {"aggregationTemporality": 1, "dataPoints": [
        pt(H20 + 4 * MS, count="6", sum=21.5, min=0.5, max=9, bucketCounts=["1", "2", "3"], explicitBounds=[1, 5])]}},
    {"name": "size", "exponentialHistogram": {"aggregationTemporality": "AGGREGATION_TEMPORALITY_DELTA", "dataPoints": [
        pt(H20 + 5 * MS, count="4", sum=10, scale=2, zeroCount="1",
           positive={"offset": -1, "bucketCounts": ["2", "1"]}, negative={"bucketCounts": []})]}},
    {"name": "rtt", "summary": {"dataPoints": [
        pt(H20 + 6 * MS, count="3", sum=3.0, quantileValues=[{"quantile": 0.5, "value": 1}, {"quantile": 0.99}])]}},
]


def test_every_metric_type_one_row_per_point(tmp_path):
    f = write(tmp_path / "a.json.gz", metrics_doc("api", ALL_TYPES))
    [w] = compact.compact("metrics", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    assert w["rows"] == 6
    got = {(r["metric_name"], r["ts_unix_nano"]): r for r in rows(w["path"])}
    g = got[("cpu", H20 + MS)]
    assert (g["metric_type"], g["value"], g["unit"], g["temporality"]) == ("gauge", 0.5, "1", None)
    assert g["attributes"] == {"host": "h1"} and g["resource_attributes"] == {"service.name": "api"}
    assert str(g["start_ts"]) == "2026-09-26 20:00:00"
    s = got[("reqs", H20 + 3 * MS)]
    assert (s["metric_type"], s["value"], s["temporality"], s["is_monotonic"], s["description"]) == (
        "sum", 42.0, 2, True, "requests")
    h = got[("latency", H20 + 4 * MS)]
    assert (h["count"], h["sum"], h["min"], h["max"], h["bucket_counts"], h["explicit_bounds"], h["temporality"]) == (
        6, 21.5, 0.5, 9.0, [1, 2, 3], [1.0, 5.0], 1)
    assert h["value"] is None
    e = got[("size", H20 + 5 * MS)]
    assert (e["exp_scale"], e["exp_zero_count"], e["exp_positive_offset"], e["exp_positive_bucket_counts"],
            e["exp_negative_bucket_counts"], e["temporality"]) == (2, 1, -1, [2, 1], [], 1)
    q = got[("rtt", H20 + 6 * MS)]
    assert (q["count"], q["sum"]) == (3, 3.0)
    assert q["quantiles"] == [{"quantile": 0.5, "value": 1.0}, {"quantile": 0.99, "value": None}]


def test_metric_points_sorted_by_metric_then_time(tmp_path):
    f = write(tmp_path / "a.json.gz", metrics_doc("api", [
        {"name": "b", "gauge": {"dataPoints": [pt(H20 + 1, asDouble=1)]}},
        {"name": "a", "gauge": {"dataPoints": [pt(H20 + 3, asDouble=1), pt(H20 + 2, asDouble=1)]}}]))
    [w] = compact.compact("metrics", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    assert [(r["metric_name"], r["ts_unix_nano"]) for r in rows(w["path"], "SELECT metric_name, ts_unix_nano FROM t")] == [
        ("a", H20 + 2), ("a", H20 + 3), ("b", H20 + 1)]
    assert (w["min_ts"], w["max_ts"]) == ("2026-09-26T20:00:00.000000Z", "2026-09-26T20:00:00.000000Z")


def test_metric_nan_and_json_numbers(tmp_path):
    f = write(tmp_path / "a.json.gz", metrics_doc("api", [
        {"name": "g", "gauge": {"dataPoints": [pt(H20, asDouble="NaN"), pt(H20 + 1, asInt=7)]}}]))
    [w] = compact.compact("metrics", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    vals = [r["value"] for r in rows(w["path"], "SELECT value FROM t ORDER BY ts_unix_nano")]
    assert vals[0] != vals[0] and vals[1] == 7.0  # NaN kept as NaN; JSON number int accepted


def test_metrics_have_no_bloom_fields(tmp_path):
    assert compact.bloom_fields("metrics") == ()
    assert compact.bloom_fields("traces", ("request.id",)) == ("trace_id", "request.id")
    f = write(tmp_path / "a.json.gz", metrics_doc("api", ALL_TYPES))
    [w] = compact.compact("metrics", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    assert w["bloom"].n == 0


def test_metrics_split_by_service_and_hour(tmp_path):
    a = write(tmp_path / "a.json.gz", metrics_doc("api", [{"name": "g", "gauge": {"dataPoints": [
        pt(H20, asDouble=1), pt(H20 + 3600 * 10**9, asDouble=2)]}}]))
    b = write(tmp_path / "b.json.gz", metrics_doc("web", [{"name": "g", "gauge": {"dataPoints": [pt(H20, asDouble=3)]}}]))
    written = compact.compact("metrics", [a, b], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    assert [(w["service"], w["hour"], w["rows"]) for w in written] == [("api", "20", 1), ("api", "21", 1), ("web", "20", 1)]


def test_unknown_signal_rejected(tmp_path):
    f = write(tmp_path / "a.json.gz", {})
    try:
        compact.compact("profiles", [f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    except ValueError as e:
        assert "profiles" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ---- blooms over ID attributes ----

def test_bloom_over_id_attributes(tmp_path):
    def rec(i, **kw):
        r = {"timeUnixNano": str(H20 + i * MS), "body": {"stringValue": "m"}, "traceId": f"{i:032x}",
             "attributes": [attr("request.id", f"R{i}"), attr("request.id", "dup"), attr("other", "x")]}
        r.update(kw)
        return r
    doc = {"resourceLogs": [
        {"resource": {"attributes": [attr("service.name", "api"), attr("service.name", "second")]},
         "scopeLogs": [{"logRecords": [rec(i) for i in range(30)] + [rec(99, timeUnixNano="0")]}]},
        {"resource": {"attributes": [attr("host", "h")]},             # no service.name
         "scopeLogs": [{"logRecords": [rec(i, traceId="") for i in range(40, 45)]}]}]}
    [a, u] = compact.compact("logs", [write(tmp_path / "a.json.gz", doc)], str(tmp_path / "o"), "b1",
                             "2026-09-26", "20", bloom_attributes=("request.id",))
    assert (a["service"], u["service"]) == ("api", "unknown")
    assert a["bloom"].might_contain(bloom.term("request.id", "r7"))
    assert not a["bloom"].might_contain(bloom.term("request.id", "dup"))   # first occurrence wins, as in the map


# ---- the fast lane's parse (spill_safe=False) gives the same rows as compaction's ----

def test_fast_and_spill_safe_parsing_give_identical_output(tmp_path):
    dup = [attr("k", "first"), attr("n", 1), attr("k", "second"), attr("request.id", "R1")]
    malformed = [{"key": "n", "value": 5}, {"key": "s", "value": "text"}, attr("ok", "v")]
    logs = {"resourceLogs": [
        {"resource": {"attributes": [attr("service.name", "api"), attr("host", "h"), attr("host", "h2"),
                                     attr("obs.route", "x")]},
         "scopeLogs": [{"logRecords": [
             {"timeUnixNano": str(H20 + i * MS), "body": {"stringValue": f"m{i}"}, "traceId": f"{i:032x}",
              "attributes": [dup, malformed, [], [attr("a", i)]][i % 4] if i % 5 else None} for i in range(40)]
             + [{"timeUnixNano": str(H20), "body": "plain", "attributes": malformed}]}]},
        {"resource": {"attributes": [attr("host", "h")]},        # no service.name
         "scopeLogs": [{"logRecords": [{"timeUnixNano": str(H20 + 3600 * 10**9), "body": {"intValue": "7"}}]}]}]}
    spans = [span(H20 + i * MS, trace=f"{i:032x}", attributes=[dup, [attr("request.id", f"q{i}")]][i % 2],
                  events=[{"timeUnixNano": str(H20 + i * MS), "name": "e", "attributes": dup}],
                  links=[{"traceId": "cd" * 16, "spanId": "ef" * 8, "attributes": dup}]) for i in range(20)]
    cases = [("logs", logs), ("traces", spans_doc("api", spans)), ("metrics", metrics_doc("api", ALL_TYPES))]
    for signal, doc in cases:
        f = write(tmp_path / f"{signal}.json.gz", doc)
        out = {}
        for safe in (True, False):
            written = compact.compact(signal, [f], str(tmp_path / f"{signal}-{safe}"), "b1", "2026-09-26", "20",
                                      bloom_attributes=("request.id",), spill_safe=safe)
            out[safe] = [(w["relpath"], w["rows"], w["bloom"].to_bytes(), rows(w["path"])) for w in written]
        assert out[True] == out[False], signal
        assert out[True] and all(r for *_, r in out[True])
