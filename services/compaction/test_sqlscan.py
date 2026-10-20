"""The SQL scan fan-out (sqlscan.py, query._run_sql_fanout): the plan it makes of a query, and that its
answers equal the single-worker path's, against moto (docs/bench/sql-classification.md)."""
import json
import math

import pytest

from test_fastlane import aws, ctx  # noqa: F401  (fixture)
from test_query import DAY, counters, data  # noqa: F401  (fixtures)
from test_promql import spans  # noqa: F401  (fixture)
import query
import sqlscan

S, E = f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z"


# ---- the plan

def test_filters_columns_and_services_of_a_simple_query():
    p = sqlscan.plan("SELECT service, count(*) FROM logs WHERE service = 'web' AND attributes['k'] = 'v' "
                     "AND severity_number >= 13 GROUP BY 1")
    assert p["logs"]["filter"] == """("service" = 'web' AND "attributes"['k'] = 'v')"""   # >= is left out: a superset
    assert p["logs"]["services"] == ["web"]
    assert {"service", "attributes", "severity_number", "ts"} <= set(p["logs"]["columns"])


def test_each_read_of_a_table_adds_its_rows_and_one_unfiltered_read_keeps_all():
    p = sqlscan.plan("SELECT (SELECT count(*) FROM metrics WHERE metric_name = 'a'), "
                     "(SELECT max(value) FROM metrics WHERE metric_name IN ('b', 'c'))")
    assert p["metrics"]["filter"] == """("metric_name" = 'a') OR ("metric_name" IN ('b', 'c'))"""
    p = sqlscan.plan("SELECT (SELECT count(*) FROM metrics WHERE metric_name = 'a'), (SELECT count(*) FROM metrics)")
    assert p["metrics"]["filter"] is None


def test_joins_keep_every_row_and_cte_names_are_not_tables():
    p = sqlscan.plan("SELECT count(*) FROM spans c JOIN spans p ON c.parent_span_id = p.span_id WHERE c.service = 'a'")
    assert p["spans"]["filter"] is None and p["spans"]["services"] is None
    p = sqlscan.plan("WITH logs AS (SELECT * FROM spans WHERE kind = 2) SELECT count(*) FROM logs")
    assert set(p) == {"spans"} and p["spans"]["filter"] == '("kind" = 2)' and p["spans"]["columns"] is None   # *


def test_conditions_that_could_name_another_query_s_columns_are_left_out():
    # x is no column of metrics: it may be the outer query's, so the condition can't filter metrics
    p = sqlscan.plan("SELECT * FROM (SELECT 1 AS x) o WHERE EXISTS (SELECT 1 FROM metrics m WHERE m.metric_name = 'a' "
                     "AND x = 1)")
    assert p["metrics"]["filter"] == """("metric_name" = 'a')"""
    p = sqlscan.plan("SELECT count(*) FROM metrics m WHERE o.metric_name = 'a'")   # another table's alias
    assert p["metrics"]["filter"] is None


@pytest.mark.parametrize("text", ["SELECT 1 FROM logs; SELECT 2 FROM logs", "SELEC 1", "SELECT * FROM read_csv('x')"])
def test_no_plan_for_what_it_does_not_analyse(text):
    assert sqlscan.plan(text) is None


def test_quotes_in_constants_stay_inside_the_literal():
    p = sqlscan.plan("SELECT count(*) FROM logs WHERE body = 'it''s'")
    assert p["logs"]["filter"] == """("body" = 'it''s')"""


# ---- answers equal the single-worker path's

def invoke(event):
    """A worker invocation, as Lambda would run it."""
    return json.loads(json.dumps(query.worker(event, None)))


def run(text, monkeypatch, fanout, in_place=False, start=S, end=E):
    monkeypatch.setenv(query.SQL_SCAN_FANOUT_FLAG, "acme" if fanout else "")
    monkeypatch.setenv(query.SQL_IN_PLACE_FLAG, "acme" if in_place else "")
    return query.run_sql("acme", text, start, end, invoke_worker=invoke)


QUERIES = [
    "SELECT service, severity_text, count(*) AS n FROM logs GROUP BY 1, 2 ORDER BY 1, 2",
    "SELECT service, count(*) FILTER (WHERE severity_number >= 13), count(DISTINCT trace_id) FROM logs "
    "WHERE service = 'web' GROUP BY 1",
    "SELECT service, round(quantile_cont(duration_ns, 0.75)), count(*) FROM spans WHERE kind IN (2, 5) GROUP BY 1 ORDER BY 1",
    "SELECT p.service, c.service, count(*) FROM spans c JOIN spans p ON c.trace_id = p.trace_id "
    "AND c.parent_span_id = p.span_id GROUP BY 1, 2 ORDER BY 1, 2",
    "WITH b AS (SELECT time_bucket(INTERVAL '60 seconds', ts) AS t, attributes['route'] AS r, sum(value) AS v FROM metrics "
    "WHERE metric_name IN ('requests', 'mem') GROUP BY 1, 2) SELECT r, count(*), sum(v) FROM b GROUP BY 1 ORDER BY 1",
    "SELECT metric_name, arg_max(value, ts), max(ts) FROM metrics WHERE metric_name = 'mem' GROUP BY 1",
    "SELECT (SELECT count(*) FROM logs WHERE service = 'api'), (SELECT count(*) FROM spans), "
    "(SELECT count(*) FROM metrics WHERE metric_name = 'requests')",
    "SELECT * FROM logs WHERE service = 'api' ORDER BY ts, body LIMIT 5",
]


def same(a, b):
    assert a["columns"] == b["columns"]
    assert len(a["rows"]) == len(b["rows"])
    for x, y in zip(a["rows"], b["rows"]):
        for u, v in zip(x, y):
            if isinstance(u, float) and isinstance(v, float):
                assert math.isclose(u, v, rel_tol=1e-9), (x, y)
            else:
                assert u == v, (x, y)


@pytest.mark.parametrize("text", QUERIES)
def test_fanout_and_in_place_answers_equal_the_single_worker_s(text, data, spans, counters, monkeypatch):
    monkeypatch.setattr(query, "TARGET_BYTES_PER_WORKER", 1)   # one slice per file: the most slices
    single = run(text, monkeypatch, fanout=False)
    fanned = run(text, monkeypatch, fanout=True)
    assert fanned["stats"]["path"] == "scan_fanout"
    assert fanned["stats"]["workers"] == fanned["stats"]["files"]   # one slice per file here
    same(single, fanned)
    same(single, run(text, monkeypatch, fanout=False, in_place=True))


def test_the_time_range_applies_in_every_slice(data, monkeypatch):
    text = "SELECT count(*), min(ts), max(ts) FROM logs"
    a, b = f"{DAY}T10:30:00Z", f"{DAY}T10:40:00Z"
    same(run(text, monkeypatch, fanout=False, start=a, end=b), run(text, monkeypatch, fanout=True, start=a, end=b))


def test_service_conditions_skip_other_services_files(data, monkeypatch):
    out = run("SELECT count(*) FROM logs WHERE service = 'web'", monkeypatch, fanout=True)
    every = run("SELECT count(*) FROM logs", monkeypatch, fanout=True)
    assert out["stats"]["files"] < every["stats"]["files"]


def test_too_big_slices_fall_back_to_the_single_worker(data, monkeypatch, capsys):
    monkeypatch.setattr(query, "SCAN_MAX_BYTES", 1)
    text = QUERIES[0]
    out = run(text, monkeypatch, fanout=True)
    assert "path" not in out["stats"] and "scan_fanout_declined" in capsys.readouterr().out
    same(out, run(text, monkeypatch, fanout=False))


def test_a_failing_scan_worker_falls_back_and_errors_are_the_single_worker_s(data, monkeypatch):
    def failing(event):
        if "scan" in event:
            raise RuntimeError("query worker failed")
        return invoke(event)
    monkeypatch.setenv(query.SQL_SCAN_FANOUT_FLAG, "acme")
    out = query.run_sql("acme", QUERIES[0], S, E, invoke_worker=failing)
    assert "path" not in out["stats"] and out["rows"]
    # a query that fails over the kept rows fails the same way on the single worker
    out = run("SELECT nope FROM logs", monkeypatch, fanout=True)
    assert "nope" in out["error"]


def test_other_accounts_keep_the_single_worker(data, monkeypatch):
    monkeypatch.setenv(query.SQL_SCAN_FANOUT_FLAG, "someone-else")
    out = query.run_sql("acme", QUERIES[0], S, E, invoke_worker=invoke)
    assert "path" not in out["stats"]
    assert query.flag_on(query.SQL_SCAN_FANOUT_FLAG, "acme") is False
    monkeypatch.setenv(query.SQL_SCAN_FANOUT_FLAG, " * ")
    assert query.flag_on(query.SQL_SCAN_FANOUT_FLAG, "acme") is True


def test_direct_calls_can_choose_the_path_and_nothing_else(data, monkeypatch):
    monkeypatch.setenv(query.SQL_SCAN_FANOUT_FLAG, "")
    out = query.run_sql("acme", QUERIES[0], S, E, invoke_worker=invoke, path="fanout")
    assert out["stats"]["path"] == "scan_fanout"
    with pytest.raises(query.BadQuery, match="sql_path"):
        query.run_sql("acme", QUERIES[0], S, E, invoke_worker=invoke, path="fast")


def test_scan_slices_hold_at_most_so_many_files(data, spans, counters, monkeypatch):
    monkeypatch.setattr(query, "SCAN_FILES_PER_WORKER", 2)
    text = "SELECT count(*) FROM metrics"
    fanned = run(text, monkeypatch, fanout=True)
    assert fanned["stats"]["workers"] == math.ceil(fanned["stats"]["files"] / 2)
    same(fanned, run(text, monkeypatch, fanout=False))


def test_small_files_are_fetched_whole_in_one_request():
    class S3:
        calls = []

        def get_object(self, Bucket, Key, Range=None):
            self.calls.append(Range or "whole")
            data = bytes(range(100))
            a, b = (int(x) for x in Range.split("=")[1].split("-")) if Range else (0, 99)
            return {"Body": __import__("io").BytesIO(data[a:b + 1])}
    s3 = S3()
    fs = query.TenantS3(s3, "b", {"k": 100}, whole_below=1000)
    for _ in range(3):   # DuckDB opens a file more than once: still one request
        with fs.open("k") as fh:
            fh.seek(90), fh.read(10), fh.seek(0)
            assert fh.read(20) == bytes(range(20))
    assert s3.calls == ["whole"]
