"""The hot store (hotstore.py) answering the recent part of questions: every answer equals the files' alone,
with the cut inside the data, across signals, counters, searches, PromQL and the SQL fan-out; and a store
that fails leaves the files to answer. Against moto."""
import gzip
import json
import math

import boto3
import pytest

from test_fastlane import aws, ctx  # noqa: F401  (fixture)
from test_query import DAY, H10, records
import hotstore
import promql
import query

S, E = f"{DAY}T10:00:00Z", f"{DAY}T10:20:00Z"
CUT_COVER = H10 // 10**9 + 5 * 60 - query.HOT_MARGIN_S   # the store covers from 10:03; the cut is 10:05


def doc(signal, service, items):
    res = {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}
    if signal == "logs":
        return {"resourceLogs": [{"resource": res, "scopeLogs": [{"logRecords": items}]}]}
    if signal == "traces":
        return {"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": items}]}]}
    return {"resourceMetrics": [{"resource": res, "scopeMetrics": [{"metrics": items}]}]}


def spans(n):
    out = []
    for i in range(n):
        t = H10 + i * 9 * 10**9
        out.append({"traceId": f"{i:032x}", "spanId": f"{i:016x}", "name": "GET /cart", "kind": 2,
                    "startTimeUnixNano": str(t), "endTimeUnixNano": str(t + (i % 50 + 1) * 10**6),
                    "status": {"code": 2} if i % 9 == 0 else {}})
    return out


def counter(minutes):
    """A cumulative counter every 10 s, rising 3 a point (one reset at minute 8)."""
    pts, v = [], 0
    for i in range(minutes * 6):
        v = 0 if i == 48 else v + 3
        pts.append({"timeUnixNano": str(H10 + i * 10 * 10**9), "startTimeUnixNano": str(H10), "asInt": str(v),
                    "attributes": [{"key": "route", "value": {"stringValue": "/cart"}}]})
    return [{"name": "requests", "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": pts}}]


@pytest.fixture
def both(aws, monkeypatch):  # noqa: F811
    """The same batches in the files (fast lane) and in a hot store; the coordinator pointed at the store."""
    handler, lookup = aws
    store = hotstore.HotStore(keep_s=10**10)
    batches = [("logs", "api", records(200, 1)), ("logs", "web", records(150, 2)),
               ("traces", "api", spans(120)), ("metrics", "api", counter(15))]
    for i, (signal, service, items) in enumerate(batches):
        body = (json.dumps(doc(signal, service, items)) + "\n").encode()
        key = f"_incoming/tenant=acme/{signal}/dt={DAY}/hour=10/b{i}.json.gz"
        boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=gzip.compress(body))
        handler.recent_indexer({"detail": {"object": {"key": key}}}, None)
        store.add("acme", signal, [gzip.compress(body)], arrived=CUT_COVER)
    monkeypatch.setattr(query, "lookup", lookup)
    monkeypatch.setattr(query, "HOT_URL", "http://hot")
    monkeypatch.setenv(query.HOT_FLAG, "acme")
    monkeypatch.setattr(query, "_hot_status", {"at": 0.0, "status": None})
    calls = []

    def hot_call(path, body, timeout=None):
        calls.append(path)
        body = json.loads(json.dumps(body))   # as sent over HTTP
        if path == "/status":
            return {**store.status(), "behind_ms": 0}
        return json.loads(json.dumps(store.worker(body) if path == "/worker" else store.scan(body)))
    monkeypatch.setattr(query, "hot_call", hot_call)
    return store, calls


def ask(q, hot):
    return query.run({"tenant": "acme", "start": S, "end": E, **q}, invoke_worker=query.run_worker, hot=hot)


def same(a, b):
    assert a["columns"] == b["columns"]
    key = lambda r: json.dumps(r, default=str)   # noqa: E731
    ra, rb = sorted(a["rows"], key=key), sorted(b["rows"], key=key)
    assert len(ra) == len(rb), (ra, rb)
    for x, y in zip(ra, rb):
        for u, v in zip(x, y):
            assert (math.isclose(u, v, rel_tol=1e-9) if isinstance(u, float) and isinstance(v, float) else u == v), (x, y)


@pytest.mark.parametrize("q", [
    {"signal": "logs", "group_by": ["service", "severity_text"], "aggs": [{"fn": "count"}]},
    {"signal": "logs", "group_by": ["ts:60"], "aggs": [{"fn": "count"}]},
    {"signal": "traces", "group_by": ["service"], "aggs": [{"fn": "count"}, {"fn": "p95", "field": "duration_ns"},
                                                         {"fn": "avg", "field": "duration_ns"}]},
    {"signal": "metrics", "group_by": ["ts:60"], "aggs": [{"fn": "increase", "field": "value"}]},
    {"signal": "metrics", "group_by": ["service"], "aggs": [{"fn": "increase", "field": "value"}]},
])
def test_answers_equal_the_files_alone(both, q):
    with_hot = ask(q, hot=True)
    assert with_hot["stats"]["hot_from"] == f"{DAY}T10:05:00.000000Z"
    same(with_hot, ask(q, hot=False))


def test_searches_take_the_latest_from_the_store(both):
    q = {"signal": "logs", "search": {"limit": 20}}
    a, b = ask(q, hot=True), ask(q, hot=False)
    assert [r for r in a["rows"]] == [r for r in b["rows"]]


def test_promql_over_the_cut(both):
    s, e = H10 // 10**9, H10 // 10**9 + 15 * 60
    text = 'sum by (service_name) (increase(requests[2m]))'
    with_hot = promql.query_range("acme", text, s, e, 60)
    query._hot_status["at"] = 0
    with pytest.MonkeyPatch.context() as m:
        m.setattr(query, "HOT_URL", "")
        files = promql.query_range("acme", text, s, e, 60)
    assert with_hot["data"] == files["data"] and with_hot["data"]["result"]   # stats differ: the store reads no files


def test_sql_fanout_and_summaries_over_the_cut(both, monkeypatch):
    monkeypatch.setenv(query.SQL_SCAN_FANOUT_FLAG, "acme")
    for text in ("SELECT service, count(*), count(DISTINCT trace_id) FROM logs GROUP BY 1",
                 "SELECT service, sum(requests)::BIGINT, sum(errors)::BIGINT, sum(duration_ns_sum)::BIGINT FROM svc_1m GROUP BY 1",
                 "SELECT time_bucket(INTERVAL 60 SECOND, ts) AS t, count(*) FROM spans GROUP BY 1"):
        with_hot = query.run_sql("acme", text, S, E, invoke_worker=lambda e: query.worker(e, None))
        assert with_hot["stats"].get("hot_from") == f"{DAY}T10:05:00.000000Z", with_hot["stats"]
        monkeypatch.setattr(query, "HOT_URL", "")
        same(with_hot, query.run_sql("acme", text, S, E, invoke_worker=lambda e: query.worker(e, None)))
        monkeypatch.setattr(query, "HOT_URL", "http://hot")


def test_a_failing_store_leaves_the_files_to_answer(both, monkeypatch, capsys):
    store, calls = both
    q = {"signal": "logs", "group_by": ["service"], "aggs": [{"fn": "count"}]}
    expected = ask(q, hot=False)
    real = query.hot_call

    def broken(path, body, timeout=None):
        if path == "/status":
            return real(path, body, timeout)
        raise ConnectionError("store down")
    monkeypatch.setattr(query, "hot_call", broken)
    out = ask(q, hot=True)
    same(out, expected)
    assert "hot_from" not in out["stats"] and "hot_failed" in capsys.readouterr().out


def test_no_store_cover_no_cut(both, monkeypatch):
    store, _ = both
    monkeypatch.setenv(query.HOT_FLAG, "someone-else")
    assert query.hot_cutoff("acme", ["logs"], E) is None
    monkeypatch.setenv(query.HOT_FLAG, "acme")
    assert query.hot_cutoff("acme", ["logs"], E) == H10 // 10**9 + 5 * 60
    assert query.hot_cutoff("acme", ["logs"], f"{DAY}T10:04:00Z") is None   # the range ends before the cut
    assert query.hot_cutoff("globex", ["logs"], E) is None   # never seen by the store


def test_old_rows_are_evicted(both):
    store, _ = both
    store.keep_s = 60
    store.evict(now=H10 // 10**9 + 17 * 60)   # keeps 10:16 on
    assert store.status()["rows"]["hot_metrics"] == 0   # the counter ends at 10:14:50
    assert store.status()["rows"]["hot_logs"] > 0       # the logs start at 10:16:40
