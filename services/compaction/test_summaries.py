"""Summaries (compact.SUMMARIES): written by the fast lane and by compaction, backfilled for older hours, and
answering the pages' questions the same as the raw data does, against moto."""
import gzip
import json

import boto3
import pytest

from test_fastlane import aws, ctx  # noqa: F401  (fixture)
from test_query import DAY, H10
import compact
import query

S, E = f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z"
NODES_RAW = """SELECT service, count(*) FILTER (WHERE kind IN (2, 5) OR parent_span_id IS NULL) AS requests,
  count(*) FILTER (WHERE (kind IN (2, 5) OR parent_span_id IS NULL) AND status_code = 2) AS errors,
  sum(duration_ns) FILTER (WHERE kind IN (2, 5) OR parent_span_id IS NULL) AS dur,
  any_value(resource_attributes['cloud.platform']) AS platform FROM spans GROUP BY 1 ORDER BY 1"""
NODES_SUM = """SELECT service, sum(requests)::BIGINT, sum(errors)::BIGINT, sum(duration_ns_sum)::BIGINT, any_value(platform)
  FROM svc_1m GROUP BY 1 ORDER BY 1"""
EDGES_RAW = """SELECT p.service AS source, c.service AS target, count(*) AS calls, count(*) FILTER (WHERE c.status_code = 2)
  FROM spans c JOIN spans p ON c.trace_id = p.trace_id AND c.parent_span_id = p.span_id
  WHERE c.service <> p.service GROUP BY 1, 2 ORDER BY 1, 2"""
EDGES_SUM = "SELECT source, target, sum(calls)::BIGINT, sum(errors)::BIGINT FROM edge_1m GROUP BY 1, 2 ORDER BY 1, 2"
WEB = """SELECT attributes['url.path'] AS page, count(*) FILTER (WHERE attributes['rum.type'] = 'page_view') AS views,
  count(DISTINCT attributes['rum.session_id']) AS visits FROM {t}
  WHERE resource_attributes['telemetry.sdk.name'] = 'leasyd-rum' AND service = 'shop-web' GROUP BY 1 ORDER BY 1"""


def sql(text):
    out = query.run_sql("acme", text, S, E, invoke_worker=query.run_sql_worker)
    assert not out.get("error"), out["error"]
    return out["rows"]


def put(signal, name, resource, records):
    key = f"_incoming/tenant=acme/{signal}/dt={DAY}/hour=10/{name}.json.gz"
    res = {"attributes": [{"key": k, "value": {"stringValue": v}} for k, v in resource.items()]}
    doc = ({"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": records}]}]} if signal == "traces"
           else {"resourceLogs": [{"resource": res, "scopeLogs": [{"logRecords": records}]}]})
    boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=gzip.compress(json.dumps(doc).encode()))
    return key


def calls(lo, hi):
    """api (Lambda) serves requests; each calls db (a child span). Every 5th db call fails."""
    api, db = [], []
    for i in range(lo, hi):
        t = H10 + i * 7 * 10**9
        api.append({"traceId": f"{i:032x}", "spanId": f"{i:08x}00000001", "name": "GET /cart", "kind": 2,
                    "startTimeUnixNano": str(t), "endTimeUnixNano": str(t + (i + 1) * 10**6)})
        db.append({"traceId": f"{i:032x}", "spanId": f"{i:08x}00000002", "parentSpanId": f"{i:08x}00000001",
                   "name": "SELECT", "kind": 3, "startTimeUnixNano": str(t), "endTimeUnixNano": str(t + 5 * 10**6),
                   "status": {"code": 2} if i % 5 == 0 else {}})
    return api, db


@pytest.fixture
def traces(aws, monkeypatch):  # noqa: F811
    """Two raw files, each holding both services' spans (as a Firehose batch does), in the fast lane."""
    handler, lookup = aws
    for name, (lo, hi) in (("a", (0, 60)), ("b", (60, 100))):
        api, db = calls(lo, hi)
        for svc, spans, res in (("api", api, {"cloud.platform": "aws_lambda"}), ("db", db, {})):
            key = put("traces", f"{name}-{svc}", {"service.name": svc, **res}, spans)
            handler.recent_indexer({"detail": {"object": {"key": key}}}, None)
    monkeypatch.setattr(query, "lookup", lookup)
    return handler


def compact_hour(handler, signal):
    hour = {"tenant": "acme", "signal": signal, "dt": DAY, "hour": "10"}
    for b in handler.dispatcher({"plan_only": hour}, ctx(0))["planned"]:
        handler.worker({**hour, "batch_id": b}, ctx(1))


def test_service_totals_equal_the_raw_spans_in_the_fast_lane_and_after_compaction(traces):
    raw = sql(NODES_RAW)
    assert raw[0][:3] == ["api", 100, 0] and raw[1][:3] == ["db", 0, 0]
    assert sql(NODES_SUM) == raw
    compact_hour(traces, "traces")
    assert sql(NODES_SUM) == raw   # the compacted summaries replace the fast lane's: nothing counted twice


def test_service_links_equal_the_raw_join_once_compacted(traces):
    # The fast lane joins within each raw file; here each service's spans came in their own file.
    compact_hour(traces, "traces")
    assert sql(EDGES_SUM) == sql(EDGES_RAW) == [["api", "db", 100, 20]]


def test_the_rum_table_holds_the_browser_events_only(aws, monkeypatch):  # noqa: F811
    handler, lookup = aws
    monkeypatch.setattr(query, "lookup", lookup)
    rum = [{"timeUnixNano": str(H10 + i * 10**9), "body": {"stringValue": "page_view"},
            "attributes": [{"key": "rum.type", "value": {"stringValue": "page_view" if i % 3 else "js_error"}},
                           {"key": "rum.session_id", "value": {"stringValue": f"s{i % 7}"}},
                           {"key": "url.path", "value": {"stringValue": f"/p{i % 4}"}}]} for i in range(90)]
    app = [{"timeUnixNano": str(H10 + i * 10**9), "body": {"stringValue": "GET /cart"}} for i in range(50)]
    for name, res, recs in (("rum", {"service.name": "shop-web", "telemetry.sdk.name": "leasyd-rum"}, rum),
                            ("app", {"service.name": "shop-web"}, app)):
        handler.recent_indexer({"detail": {"object": {"key": put("logs", name, res, recs)}}}, None)
    assert sql("SELECT count(*) FROM rum") == [[90]]
    assert sql(WEB.format(t="rum")) == sql(WEB.format(t="logs"))
    compact_hour(handler, "logs")
    assert sql("SELECT count(*) FROM rum") == [[90]]
    assert sql(WEB.format(t="rum")) == sql(WEB.format(t="logs"))


def test_backfill_builds_summaries_of_hours_compacted_before_they_existed(traces, monkeypatch):
    raw = sql(NODES_RAW)
    with monkeypatch.context() as m:   # compacted before summaries existed
        m.setattr(compact, "SUMMARIES", {})
        compact_hour(traces, "traces")
    assert sql(NODES_SUM) == [] and sql(EDGES_SUM) == []
    first = traces.summaries_backfill({"tenant": "acme", "day": DAY})["written"]
    assert sorted(first) == [f"edge_1m {DAY}T10", f"svc_1m {DAY}T10"]
    assert sql(NODES_SUM) == raw and sql(EDGES_SUM) == sql(EDGES_RAW)
    assert sorted(traces.summaries_backfill({"tenant": "acme", "day": DAY})["written"]) == sorted(first)   # rewritten
    assert sql(NODES_SUM) == raw and sql(EDGES_SUM) == sql(EDGES_RAW)   # not counted twice


def test_backfill_fills_an_hour_that_already_has_some_summaries(traces, monkeypatch):
    """Found 2026-10-20: late spans of an hour compacted before summaries existed got fast-lane summaries
    after the deploy, and the backfill took the hour for done. It now rebuilds every compacted hour."""
    with monkeypatch.context() as m:
        m.setattr(compact, "SUMMARIES", {})
        compact_hour(traces, "traces")
    api, db = calls(100, 103)   # late spans of the same hour, arriving after summaries exist
    for svc, spans in (("api", api), ("db", db)):
        traces.recent_indexer({"detail": {"object": {"key": put("traces", f"late-{svc}", {"service.name": svc}, spans)}}}, None)
    raw = sql(NODES_RAW)
    assert sql(NODES_SUM) != raw   # only the late spans are summarized
    traces.summaries_backfill({"tenant": "acme", "day": DAY})
    assert sql(NODES_SUM) == raw


def test_an_empty_summary_table_still_has_its_columns(aws, monkeypatch):  # noqa: F811
    monkeypatch.setattr(query, "lookup", aws[1])
    assert sql("SELECT count(*), sum(requests) FROM svc_1m") == [[0, None]]
    assert sql("SELECT count(*) FROM edge_1m e JOIN rum r ON e.source = r.service") == [[0]]


def test_the_retention_sweeper_knows_every_summary_table():
    import pathlib
    import re
    admin = (pathlib.Path(__file__).parent.parent / "tenants" / "admin.py").read_text()
    listed = re.search(r"SUMMARY_TABLES = \(([^)]*)\)", admin).group(1)
    assert {t.strip(' "') for t in listed.split(",") if t.strip()} == set(compact.SUMMARIES)
