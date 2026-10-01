"""PromQL (promql.py) over the query engine against moto, checked against known data."""
import gzip
import json
import math

import boto3
import pytest

from test_fastlane import aws, ctx  # noqa: F401  (fixture)
from test_query import DAY, H10, counters, data, expected_rise, metric_points  # noqa: F401  (fixtures)
import query
import promql

T10 = H10 // 10**9   # 2026-09-26T10:00:00Z, epoch seconds


def run_query(q):
    return query.run(q, invoke_worker=query.run_worker)


def prange(text, start, end, step=60):
    out = promql.query_range("acme", text, start, end, step, run_query=run_query)
    return {tuple(sorted(s["metric"].items())): {t: float(v) for t, v in s["values"]} for s in out["data"]["result"]}


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize("text", [
    'sum by (service_name) (rate(leasyd.spans{span_kind="SERVER"}[5m]))',
    'histogram_quantile(0.95, sum by ("service.name") (rate(leasyd.span.duration[5m]))) * 1000',
    'rate({"http.server.requests", "http.route"=~"/api/.*"}[1h30m] offset 5m) > bool 2',
    'sum(rate(a_total[1m])) without (x)', '1 + 2 * 3 ^ 2 ^ 2', 'topk(3, x) or on(a) y unless ignoring(b) z',
    '-x', 'clamp(x, 0, 1)', "# a comment\nx{a='b',}",
])
def test_parses(text):
    promql.parse(text)


def test_precedence_and_associativity():
    assert promql.parse("1 + 2 * 3")[0:2] == ("binary", "+")
    right = promql.parse("2 ^ 3 ^ 2")
    assert right[2] == ("num", 2.0) and right[3][1] == "^"   # ^ is right-associative


@pytest.mark.parametrize("text, msg", [
    ("rate(x[5m]", r"expected , or \)"), ("x[5m]", None), ("foo(x)", "unknown function"), ("{a=\"b\"}", "metric name"),
    ("x[5m:1m]", "subqueries"), ("a / on(x) group_left b", "group_left"), ("x{a~\"b\"}", "unexpected character"),
    ("sum(x) by (a) by (b)", None),
])
def test_bad_queries(text, msg):
    with pytest.raises(promql.PromQLError, match=msg):
        ast = promql.parse(text)
        promql.Evaluator("acme", 0, 60, 60, run_query=lambda q: {"rows": [], "columns": []}).eval(ast)


# ------------------------------------------------------------------ metrics

def test_counter_rate_per_minute_is_exact(counters):
    got = prange('rate(reqs{route="/a"}[1m])', T10 + 60, T10 + 600)
    ((labels, series),) = got.items()
    assert dict(labels)["route"] == "/a" and dict(labels)["service_name"] == "api"
    want = expected_rise(counters, "reqs", "/a")
    for t, v in series.items():   # the window (t-60, t] is the bucket starting at t-60
        bucket = f"{__import__('time').strftime('%Y-%m-%dT%H:%M:%S', __import__('time').gmtime(t - 60))}.000000Z"
        assert v == pytest.approx(want[bucket] / 60)
    # the restart at 10:05 counts its rise, never a negative jump
    assert series[T10 + 360] == pytest.approx((3 + 5 * 5) / 60)


def test_sum_by_is_pushed_down_and_equals_summing_series(counters):
    per_series = prange('rate(reqs[5m])', T10 + 300, T10 + 600)
    total = prange('sum(rate(reqs[5m]))', T10 + 300, T10 + 600)
    ((_, s),) = total.items()
    for t in s:
        assert s[t] == pytest.approx(sum(x.get(t, 0) for x in per_series.values()))
    by_route = prange('sum by (route) (increase(reqs[5m]))', T10 + 300, T10 + 600)
    assert {dict(k)["route"] for k in by_route} == {"/a", "/b"}
    # an underscore name finds the dotted attribute key too
    assert prange('sum by (route) (increase(reqs_total[5m]))', T10 + 300, T10 + 600) == by_route


def test_gauges_instant_and_over_time(counters):
    mem = counters[("mem", None)]
    got = prange("mem", T10 + 120, T10 + 120)
    ((labels, s),) = got.items()
    assert dict(labels)["__name__"] == "mem"
    assert s[T10 + 120] == max(v for ts, v in mem if ts < (T10 + 120) * 10**9)   # latest point before t (mem rises)
    avg = prange("avg_over_time(mem[1m])", T10 + 120, T10 + 120)
    pts = [v for ts, v in mem if (T10 + 60) * 10**9 <= ts < (T10 + 120) * 10**9]
    assert next(iter(avg.values()))[T10 + 120] == pytest.approx(sum(pts) / len(pts))
    mx = prange("max_over_time(mem[5m]) - min_over_time(mem[5m])", T10 + 300, T10 + 300)
    assert next(iter(mx.values()))[T10 + 300] == pytest.approx(29.0)   # 30 points, +1 each
    assert prange("count_over_time(mem[1m])", T10 + 60, T10 + 60) and \
        next(iter(prange("count_over_time(mem[1m])", T10 + 60, T10 + 60).values()))[T10 + 60] == 6


def test_arithmetic_comparisons_and_matching(counters):
    base = 'sum by (route) (increase(reqs[5m]))'
    both = prange(f"{base} / on(route) {base}", T10 + 300, T10 + 300)
    assert all(v == 1 for s in both.values() for v in s.values())
    doubled = prange(f"2 * {base}", T10 + 300, T10 + 300)
    one = prange(base, T10 + 300, T10 + 300)
    assert {k: {t: v * 2 for t, v in s.items()} for k, s in one.items()} == doubled
    top = max(v for s in one.values() for v in s.values())
    assert len(prange(f"{base} >= {top}", T10 + 300, T10 + 300)) == 1
    assert {v for s in prange(f"{base} >= bool {top}", T10 + 300, T10 + 300).values() for v in s.values()} == {0, 1}
    assert len(prange(f"topk(1, {base})", T10 + 300, T10 + 300)) == 1
    assert prange("vector(1) + 1", T10, T10) == {(): {T10: 2.0}}
    only_a = 'sum by (route) (increase(reqs{route="/a"}[5m]))'
    assert prange(f"{base} and on(route) {only_a}", T10 + 300, T10 + 300).keys() == \
        {k for k in one if dict(k)["route"] == "/a"}


# ------------------------------------------------------------------ logs and spans

def test_logs_by_severity(data):
    got = prange('sum by (severity_text) (increase(leasyd.logs[1h]))', T10 + 3600, T10 + 3600, step=3600)
    want = {}
    for r in data:
        want[r["severityText"]] = want.get(r["severityText"], 0) + 1
    assert {dict(k)["severity_text"]: s[T10 + 3600] for k, s in got.items()} == want
    errors = prange('sum(increase(leasyd.logs{severity_range="ERROR_FATAL"}[1h]))', T10 + 3600, T10 + 3600, step=3600)
    assert next(iter(errors.values()))[T10 + 3600] == want["ERROR"]
    by_service = prange('increase(leasyd.logs[1h])', T10 + 3600, T10 + 3600, step=3600)
    assert {dict(k)["service_name"] for k in by_service} == {"api", "web"}
    with pytest.raises(promql.PromQLError, match="counts records"):
        prange("leasyd.logs", T10, T10)


@pytest.fixture
def spans(aws, monkeypatch):  # noqa: F811
    """200 spans in 10:00-10:10: api (SERVER, 1..100 ms, every 10th an error) and db (CLIENT, 200 ms)."""
    handler, lookup = aws
    out = []
    for i in range(100):
        start = H10 + i * 6 * 10**9
        for svc, kind, dur_ms in (("api", 2, i + 1), ("db", 3, 200)):
            out.append((svc, {"traceId": f"{i:032x}", "spanId": f"{i:08x}{kind:08x}", "name": "GET /cart" if svc == "api" else "SELECT",
                              "kind": kind, "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + dur_ms * 10**6),
                              "status": {"code": 2} if svc == "api" and i % 10 == 0 else {}}))
    for svc in ("api", "db"):
        doc = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                                  "scopeSpans": [{"spans": [s for v, s in out if v == svc]}]}]}
        key = f"_incoming/tenant=acme/traces/dt={DAY}/hour=10/{svc}.json.gz"
        boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=gzip.compress(json.dumps(doc).encode()))
        handler.recent_indexer({"detail": {"object": {"key": key}}}, None)
    monkeypatch.setattr(query, "lookup", lookup)
    return out


def test_span_rates_errors_and_latency(spans):
    at = T10 + 600
    rate = prange('sum by (service_name, span_kind) (increase(leasyd.spans[10m]))', at, at)
    assert {(dict(k)["service_name"], dict(k)["span_kind"]): s[at] for k, s in rate.items()} == \
        {("api", "SERVER"): 100, ("db", "CLIENT"): 100}
    err = prange('sum(rate(leasyd.spans{status_code="ERROR"}[10m])) / sum(rate(leasyd.spans{service_name="api"}[10m]))', at, at)
    assert next(iter(err.values()))[at] == pytest.approx(0.1)
    p50 = prange('histogram_quantile(0.5, sum by (service_name) (rate(leasyd.span.duration[10m])))', at, at)
    got = {dict(k)["service_name"]: s[at] for k, s in p50.items()}
    assert got["api"] == pytest.approx(0.050, rel=0.05) and got["db"] == pytest.approx(0.200, rel=0.05)   # seconds
    p99 = prange('histogram_quantile(0.99, rate(leasyd.span.duration{service_name="api"}[10m])) * 1000', at, at)
    assert next(iter(p99.values()))[at] == pytest.approx(99, rel=0.05)
    names = prange('count by (span_name) (increase(leasyd.spans[10m]))', at, at)
    assert {dict(k)["span_name"] for k in names} == {"GET /cart", "SELECT"}


def test_limits():
    with pytest.raises(promql.PromQLError, match="multiple of 10"):
        promql.query_range("acme", "vector(1)", 0, 600, 15)
    with pytest.raises(promql.PromQLError, match="too many points"):
        promql.query_range("acme", "vector(1)", 0, 10 * 86400, 10)
    with pytest.raises(promql.PromQLError, match="at most 20"):
        promql.parse(" + ".join(["x"] * 21))


def test_api(counters, monkeypatch):
    import base64
    monkeypatch.setattr(query, "RETENTION_DAYS", 10000)
    monkeypatch.setattr(query, "_invoke_worker", query.run_worker)

    def call(body, tenant="acme"):
        ev = {"resource": "/v1/app/query", "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant}}},
              "body": base64.b64encode(json.dumps(body).encode()).decode(), "isBase64Encoded": True}
        r = query.api(ev, None)
        return r["statusCode"], json.loads(r["body"])
    st, out = call({"promql": "sum(rate(reqs[1m]))", "start": f"{DAY}T10:02:00Z", "end": T10 + 300, "step": "60"})
    assert st == 200 and out["status"] == "success" and out["data"]["resultType"] == "matrix"
    assert [v[0] for v in out["data"]["result"][0]["values"]] == list(range(T10 + 120, T10 + 301, 60))
    st, out = call({"promql": "mem", "time": T10 + 120})
    assert st == 200 and out["data"]["resultType"] == "vector" and out["data"]["result"][0]["metric"]["__name__"] == "mem"
    st, out = call({"promql": "sum(rate(reqs[1m]))", "time": T10 + 120}, tenant="globex")
    assert st == 200 and out["data"]["result"] == []            # another tenant sees nothing
    st, out = call({"promql": "rate(reqs[1m]", "time": T10})
    assert st == 400 and out["status"] == "error" and "expected" in out["error"]


def test_every_series_has_service_name_and_labels_match_by_either_spelling(counters):
    per_series = prange("avg by (service_name) (avg_over_time(mem[5m]))", T10 + 300, T10 + 300)   # not pushed down
    assert [dict(k) for k in per_series] == [{"service_name": "api"}]
    assert prange('avg by ("service.name") (avg_over_time(mem[5m]))', T10 + 300, T10 + 300).keys() == \
        {(("service.name", "api"),)}
    a = prange("max by (route) (max_over_time(reqs[5m]))", T10 + 300, T10 + 300)
    b = prange("sum by (route) (increase(reqs[5m]))", T10 + 300, T10 + 300)   # pushed down
    assert a.keys() == b.keys()
    assert len(prange("max by (route) (max_over_time(reqs[5m])) / on(route) sum by (route) (increase(reqs[5m]))", T10 + 300, T10 + 300)) == 2


def test_direct_invocation_for_alerts(counters, monkeypatch):
    monkeypatch.setattr(query, "_invoke_worker", query.run_worker)
    out = query.handler({"tenant": "acme", "promql": "sum by (route) (increase(reqs[5m]))", "time": T10 + 300}, None)
    assert out["data"]["resultType"] == "vector" and {r["metric"]["route"] for r in out["data"]["result"]} == {"/a", "/b"}
    assert "error" in query.handler({"tenant": "acme", "promql": "rate(", "time": T10}, None)
