"""Read-only SQL over a tenant's data (query.run_sql), against moto."""
import pytest

from test_fastlane import aws, ctx  # noqa: F401  (fixture)
from test_query import DAY, counters, data  # noqa: F401  (fixtures)
from test_promql import spans  # noqa: F401  (fixture)
import query

S, E = f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z"


def sql(text, tenant="acme", start=S, end=E, services=None):
    out = query.run_sql(tenant, text, start, end, invoke_worker=query.run_sql_worker, services=services)
    if out.get("error"):
        raise query.BadQuery(out["error"])
    return out


def test_queries_all_tables_and_joins(data, spans, counters):
    out = sql("SELECT severity_text, count(*) AS n FROM logs GROUP BY 1 ORDER BY 1")
    assert out["columns"] == ["severity_text", "n"] and sum(r[1] for r in out["rows"]) == len(data)
    out = sql("""WITH slow AS (SELECT service, quantile_cont(duration_ns, 0.5) / 1e6 AS p50_ms FROM spans GROUP BY 1)
                 SELECT s.service, round(p50_ms) FROM slow s ORDER BY 1""")
    assert out["rows"] == [["api", 50.0], ["db", 200.0]] or out["rows"][1] == ["db", 200.0]
    out = sql("SELECT metric_name, count(*) FROM metrics GROUP BY 1 ORDER BY 1")
    assert dict((r[0], r[1]) for r in out["rows"])["mem"] == 60
    # logs joined to spans by trace id (none share one here), and the time range applies
    assert sql("SELECT count(*) FROM logs l JOIN spans s USING (trace_id)")["rows"] == [[0]]
    assert sql("SELECT count(*) FROM logs", start=f"{DAY}T10:30:00Z", end=f"{DAY}T10:40:00Z")["rows"][0][0] < len(data)


def test_services_hint_reads_only_those_services(data):
    every = dict(sql("SELECT service, count(*) FROM logs GROUP BY 1")["rows"])
    assert set(every) == {"api", "web"}
    assert sql("SELECT service, count(*) FROM logs GROUP BY 1", services=["web"])["rows"] == [["web", every["web"]]]
    assert sql("SELECT count(*) FROM logs", services=["nope"])["rows"] == [[0]]
    for bad in ([], "web", [1], ["s"] * 51):
        with pytest.raises(query.BadQuery, match="services"):
            sql("SELECT 1 FROM logs", services=bad)


def test_other_tenants_see_nothing(data):
    assert sql("SELECT count(*) FROM logs", tenant="nobody")["rows"] == [[0]]


@pytest.mark.parametrize("text, msg", [
    ("DELETE FROM logs", "only one SELECT"),
    ("CREATE TABLE x AS SELECT 1", "only one SELECT"),
    ("SELECT 1; SELECT 2", "exactly one"),
    ("SELECT * FROM read_csv('/etc/passwd')", "table functions"),
    ("SELECT * FROM read_text('/proc/self/environ')", "table functions"),
    ("SELECT * FROM duckdb_settings()", "table functions"),
    ("SELECT * FROM users", "unknown table"),
    ("SELECT * FROM main.logs", "unknown table"),
    ("COPY (SELECT 1) TO '/tmp/x.csv'", "only one SELECT"),
    ("ATTACH '/tmp/y.db'", "only one SELECT"),
    ("SET lock_configuration=false", "only one SELECT"),
    ("SELECT 1", "at least one"),
    ("SELEC 1", "bad SQL"),
])
def test_refused(text, msg):
    with pytest.raises(query.BadQuery, match=msg):
        query.check_sql(text)


def test_cte_names_only_count_where_they_are_visible():
    """A CTE defined inside one subquery doesn't make that name a table elsewhere in the query
    (found 2026-10-17: it let a file path or the worker's raw temp tables through the checker)."""
    for text in ('SELECT * FROM logs, (WITH "/etc/passwd" AS (SELECT 1) SELECT * FROM "/etc/passwd") a, "/etc/passwd" b',
                 "SELECT * FROM logs WHERE ts IN (WITH raw_logs_0 AS (SELECT 1 AS ts) SELECT ts FROM raw_logs_0) "
                 "UNION ALL SELECT * FROM raw_logs_0"):
        with pytest.raises(query.BadQuery, match="unknown table"):
            query.check_sql(text)
    # still fine: a CTE used in its own query, in a subquery of it, and recursively
    assert query.check_sql("WITH e AS (SELECT * FROM logs) SELECT count(*) FROM e") == ["logs"]
    assert query.check_sql("WITH e AS (SELECT * FROM logs) SELECT (SELECT count(*) FROM e)") == ["logs"]
    assert query.check_sql("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
                           "SELECT * FROM r, (SELECT count(*) FROM spans) s") == ["spans"]


def test_deeply_nested_queries_are_refused_not_crashed():
    """A 2 KB query nested deeply enough crashed the parser's JSON walk (RecursionError: a 503 and
    an alarm, repeatable); it is a bad query."""
    for text in ("SELECT " + "+".join(["1"] * 800) + " FROM logs",
                 "SELECT * FROM " + "(SELECT * FROM " * 400 + "logs" + ")" * 400):
        with pytest.raises(query.BadQuery, match="nested too deeply"):
            query.check_sql(text)


def test_locked_down_even_past_the_checker(data, monkeypatch):
    """If a query slipped past check_sql, DuckDB itself still refuses files and settings."""
    monkeypatch.setattr(query, "check_sql", lambda s: ["logs"])
    for text in ("SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM read_text('/proc/self/environ')",
                 "SELECT * FROM read_parquet('s3://obs-data-test/**/*.parquet')", "SET lock_configuration=false",
                 "INSTALL httpfs", "COPY (SELECT 1) TO '/tmp/leak.csv'"):
        out = query.run_sql("acme", text, S, E, invoke_worker=query.run_sql_worker)
        assert out.get("error") and ("Permission" in out["error"] or "lock" in out["error"] or "disabled" in out["error"]), (text, out)
    assert query.run_sql("acme", "SELECT count(*) FROM logs", S, E, invoke_worker=query.run_sql_worker)["rows"] == [[len(data)]]


def test_api_takes_iso_or_epoch_times(monkeypatch):
    seen = []
    monkeypatch.setattr(query, "kept_from", lambda: query.datetime(2000, 1, 1, tzinfo=query.timezone.utc))
    monkeypatch.setattr(query, "run_sql", lambda tenant, text, start, end, services=None: seen.append((start, end)) or {"columns": [], "rows": []})
    for start, end in ((f"{DAY}T10:00:00Z", f"{DAY}T11:00:00Z"), (1767261600, "1767265200")):
        assert query._sql_api("acme", {"sql": "SELECT 1", "start": start, "end": end})["statusCode"] == 200
    assert seen[0] == (f"{DAY}T10:00:00Z", f"{DAY}T11:00:00Z") and seen[1] == ("2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z")
    assert query._sql_api("acme", {"sql": "SELECT 1", "start": "yesterday", "end": E})["statusCode"] == 400
    assert query._sql_api("acme", {"sql": "SELECT 1", "start": str(1767261600 * 10**9), "end": E})["statusCode"] == 400   # nanoseconds
