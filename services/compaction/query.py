"""Query engine (Phases 4-5): answer a query over one tenant's data.

    {"tenant": "acme",                         # required (from the caller's credentials in an API)
     "signal": "logs",                         # logs | traces | metrics
     "start": "2026-09-27T00:00:00Z", "end": "2026-09-27T23:59:59Z",
     "services": ["checkout"],                 # optional; default every service
     "where": [{"field": "severity_number", "op": ">=", "value": 17},
               {"field": "attributes.http.route", "op": "=", "value": "/api/cart"},
               {"field": "body", "op": "contains", "value": "timeout"}],
     "match": {"trace_id": "4bf9..."},         # optional ID lookup (bloom-pruned, then filtered exactly)
     "group_by": ["attributes.http.route"],    # aggregate mode (or "ts:60": per-minute buckets) ...
     "aggs": [{"fn": "count"}, {"fn": "p95", "field": "attributes.duration_ms"}],
     "search": {"limit": 100},                 # ... or search mode: newest matching rows
     "order": "desc", "limit": 100,            # aggregate results: by the first agg
     "workers": 16}                            # most parallel workers to use

Fields: a column of the signal's table (e.g. severity_number, body, name,
duration_ns, metric_name, value), or attributes.<key> / resource.<key>.
Ops: = != < <= > >= in contains exists. Aggregates: count, sum, min, max,
avg, p50, p90, p95, p99 (percentiles from a log-bucket histogram, ~2.5%
relative error, so they merge exactly across workers), and for metrics
increase (of value, count or sum): how much a counter went up. A delta point
counts as it is; a cumulative point counts its rise since the series' previous
point (a drop means the counter restarted from zero, so the whole value counts).
A series is one metric of one service with one set of attributes and resource
attributes. Divide by the bucket length for a rate per second.
buckets (metric histograms, the only aggregate of its query): per group, how many
measurements fell in each of the histogram's buckets over the range, as
{upper bound: count} ("+Inf" for the last), with cumulative points taken as rises
like increase. For PromQL's histogram_quantile.

The query is never SQL from the caller: fields are checked against the
signal's columns and every value is a bound parameter.

coordinator: runs the index lookup (the files that can hold matching rows,
compacted and fast lane), splits them into balanced chunks, runs one worker per
chunk in parallel, and merges their partial results.
worker: downloads its files with the tenant-scoped credentials (IAM refuses
anything outside the tenant), loads them into DuckDB, runs the compiled
query and returns partial aggregates or rows.
"""

import base64
import json
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal

import boto3
import botocore.config
from botocore.exceptions import ClientError
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

import compact
import layout
import lookup

WORKER_FUNCTION = os.environ.get("QUERY_WORKER_FUNCTION", "obs-query-worker")
MAX_WORKERS = int(os.environ.get("QUERY_MAX_WORKERS", "64"))
# Data is kept this many full days plus today (UTC); the tenant admin's daily retention job deletes
# earlier days. Customers' queries start no earlier, so a sweep in progress never shows as gaps.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
MAX_IN_VALUES = 2000
TARGET_BYTES_PER_WORKER = int(os.environ.get("QUERY_BYTES_PER_WORKER", str(256 * 1024 * 1024)))
# Opening a file costs about as much as reading this many more bytes (footer and column-chunk
# requests; ~9 ms a file measured on 3,000 small metrics files), so many small files also get
# spread over workers, not only large ones.
FILE_COST_BYTES = int(os.environ.get("QUERY_FILE_COST_BYTES", str(1024 * 1024)))
DOWNLOAD_THREADS = 32
READ_MODE = os.environ.get("QUERY_READ_MODE", "ranges")   # or "download"
RANGE_THREADS = int(os.environ.get("QUERY_RANGE_THREADS", "8"))
RANGE_BLOCK_BYTES = 256 * 1024   # read-ahead per S3 range request (small: column chunks can be tiny)
MAX_ROWS = 10_000          # search results and aggregate groups returned
HIST_BASE = 1.05           # percentile buckets: relative error <= 2.5%

COLUMNS = {
    "logs": {"ts", "ts_unix_nano", "observed_ts", "service", "severity_number", "severity_text", "body",
             "trace_id", "span_id", "scope_name"},
    "traces": {"ts", "ts_unix_nano", "end_ts", "duration_ns", "service", "name", "kind", "status_code",
               "status_message", "trace_id", "span_id", "parent_span_id", "trace_state", "scope_name"},
    "metrics": {"ts", "ts_unix_nano", "start_ts", "service", "metric_name", "metric_type", "unit", "description",
                "temporality", "is_monotonic", "value", "count", "sum", "min", "max", "flags", "scope_name"},
}
OPS = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
AGGS = {"count", "sum", "min", "max", "avg", "p50", "p90", "p95", "p99", "increase",
        "last",   # the value of the latest point in the group
        "hist",   # the percentile histogram itself ({bucket: count}, HIST_BASE buckets), to merge later
        "buckets"}   # metric histograms: {upper bound "le": count} over the range (rises, as increase)
_PCT = re.compile(r"p(\d{1,2}(?:\.\d{1,3})?)")   # any percentile: p50, p99.9, ...
MAX_INTERNAL_ROWS = 50_000   # "max_rows" (set by PromQL, not the API): groups a query may return
INCREASE_FIELDS = {"value", "count", "sum"}   # metrics columns a counter's rise can be taken of
DELTA, CUMULATIVE = 1, 2                       # metrics "temporality" (OTLP AggregationTemporality)
_ATTR_KEY = re.compile(r"^[A-Za-z0-9_.\-/:]{1,128}$")

lam = boto3.client("lambda", config=botocore.config.Config(
    max_pool_connections=MAX_WORKERS, read_timeout=900, retries={"max_attempts": 0}))


class BadQuery(ValueError):
    pass


# ------------------------------------------------------------ compile

def _field(signal, name, params):
    """SQL for a field; attribute keys become bound parameters."""
    if not isinstance(name, str):
        raise BadQuery(f"bad field {name!r}")
    if name.startswith("label."):
        # A PromQL-style label: an attribute, else a resource attribute; "a|b" tries each key in turn.
        keys = name[len("label."):].split("|")
        if not 1 <= len(keys) <= 4 or not all(_ATTR_KEY.match(k) for k in keys):
            raise BadQuery(f"bad label {name[len('label.'):]!r}")
        params += keys + keys
        return "coalesce(" + ", ".join([f"attributes[?]"] * len(keys) + [f"resource_attributes[?]"] * len(keys)) + ")"
    for prefix, col in (("attributes.", "attributes"), ("resource.", "resource_attributes")):
        if name.startswith(prefix):
            key = name[len(prefix):]
            if not _ATTR_KEY.match(key):
                raise BadQuery(f"bad attribute key {key!r}")
            params.append(key)
            return f"{col}[?]"
    if name not in COLUMNS[signal]:
        raise BadQuery(f"unknown field {name!r} for {signal}; one of {sorted(COLUMNS[signal])} "
                       "or attributes.<key> / resource.<key>")
    return f'"{name}"'


BUCKET_SECONDS = {10, 30, 60, 300, 900, 1800, 3600, 7200, 21600, 43200, 86400}


def _time_bucket(name):
    """group_by "ts:<seconds>": the start of each time bucket (a time series)."""
    m = re.fullmatch(r"ts:(\d+)", name) if isinstance(name, str) else None
    if not m:
        return None
    if int(m[1]) not in BUCKET_SECONDS:
        raise BadQuery(f"time bucket must be one of {sorted(BUCKET_SECONDS)} seconds")
    return f"time_bucket(INTERVAL '{int(m[1])} seconds', ts)"


LOG_STEPS = 2                # group_by "log:<field>": buckets per doubling (…, 707 ms, 1 s, 1.41 s, …)
LOG_BASE = 2 ** (1 / LOG_STEPS)
_BUCKETABLE = {"duration_ns", "value", "severity_number"}
_HASHABLE = {"attributes", "resource_attributes", "series"}
_SORTED_JSON = "to_json(map_from_entries(list_sort(map_entries({}))))::VARCHAR"   # key order never matters


def _derived(signal, name):
    """group_by "log:<field>": the log bucket of a number (its lower bound is LOG_BASE**bucket);
    "hash:<attributes|resource_attributes>": one key per distinct attribute set (a series)."""
    m = re.fullmatch(r"(log|hash|json):([a-z_]+)", name) if isinstance(name, str) else None
    if not m:
        return None
    kind, field = m.groups()
    if kind == "log":
        if field not in _BUCKETABLE or field not in COLUMNS[signal]:
            raise BadQuery(f"log buckets are for {sorted(_BUCKETABLE & COLUMNS[signal])}")
        x = _num(f'"{field}"')
        return f"CASE WHEN {x} > 0 THEN floor(log2({x}) * {LOG_STEPS})::INTEGER END"   # log2: exact at powers of 2
    if kind == "json":   # the attribute map itself, as JSON with sorted keys (a series' labels)
        if field not in ("attributes", "resource_attributes"):
            raise BadQuery("json is for attributes or resource_attributes")
        return _SORTED_JSON.format(f'"{field}"')
    if field not in _HASHABLE:
        raise BadQuery(f"hash is for {sorted(_HASHABLE)}")
    if field == "series":   # one key per series: service, metric and both attribute maps
        if signal != "metrics":
            raise BadQuery("hash:series is for metrics")
        return (f"hash(concat_ws('|', service, metric_name, {_SORTED_JSON.format('attributes')}, "
                f"{_SORTED_JSON.format('resource_attributes')}))::VARCHAR")
    return f'hash("{field}"::VARCHAR)::VARCHAR'


def _regex(v):
    if not isinstance(v, str) or len(v) > 1000:
        raise BadQuery("a regex must be a string of at most 1000 characters")
    try:
        regex_compile_check(v)
    except re.error as e:
        raise BadQuery(f"bad regex {v!r}: {e}")
    return v


def regex_compile_check(v):
    re.compile(v)


def _num(expr):
    return f"TRY_CAST({expr} AS DOUBLE)"


def compile_query(q, edges=False):
    """-> (sql, params, kind) for a worker. kind: "search" or "aggregate".
    edges: instead (sql, params) of each counter series' first and last point in the worker's
    files, for increase queries (None otherwise): merge() adds the rise between one worker's
    last point and the next worker's first, which neither worker sees."""
    signal = q.get("signal", "logs")
    if signal not in COLUMNS:
        raise BadQuery(f"unknown signal {signal!r}")
    params = []
    where = ["ts >= ?::TIMESTAMP", "ts <= ?::TIMESTAMP"]
    params += [_naive(q["start"]), _naive(q["end"])]
    if q.get("services"):
        where.append(f"service IN ({', '.join('?' * len(q['services']))})")
        params += list(q["services"])
    for f, v in sorted((q.get("match") or {}).items()):
        where.append(f"lower({_field(signal, f if f == 'trace_id' else 'attributes.' + f, params)}) = lower(?)")
        params.append(str(v))
    for cond in q.get("where") or []:
        op = cond.get("op", "=")
        expr = _field(signal, cond.get("field"), params)
        if op == "exists":
            where.append(f"{expr} IS NOT NULL")
        elif op == "not_exists":
            where.append(f"{expr} IS NULL")
        elif op in ("regex", "not_regex"):   # PromQL =~ / !~: the whole value must match; missing is ""
            where.append(f"regexp_full_match(coalesce({expr}::VARCHAR, ''), ?)" + (" IS NOT TRUE" if op == "not_regex" else ""))
            params.append(_regex(cond.get("value")))
        elif op == "contains":
            where.append(f"{expr}::VARCHAR ILIKE ?")
            params.append("%" + str(cond["value"]).replace("%", r"\%").replace("_", r"\_") + "%")
        elif op == "in":
            vals = list(cond["value"])
            if not vals:
                raise BadQuery("'in' needs values")
            where.append(f"{expr}::VARCHAR IN ({', '.join('?' * len(vals))})")
            params += [str(v) for v in vals]
        elif op == "not_in":            # a missing value is "not in" (rows without the field stay)
            vals = list(cond["value"])
            if len(vals) > MAX_IN_VALUES:
                raise BadQuery(f"'not_in' takes at most {MAX_IN_VALUES} values")
            if vals:
                where.append(f"({expr}::VARCHAR IN ({', '.join('?' * len(vals))})) IS NOT TRUE")
                params += [str(v) for v in vals]
            else:                         # nothing to leave out (still uses the field's bound key)
                where.append(f"({expr} IS NULL OR TRUE)")
        elif op in OPS:
            v = cond["value"]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                where.append(f"{_num(expr)} {OPS[op]} ?")
                params.append(float(v))
            else:
                where.append(f"{expr}::VARCHAR {OPS[op]} ?")
                params.append(str(v))
        else:
            raise BadQuery(f"unknown op {op!r}")
    where_sql = " AND ".join(where)

    if q.get("search") is not None:
        limit = min(int((q.get("search") or {}).get("limit", 100)), MAX_ROWS)
        return f"SELECT * FROM t WHERE {where_sql} ORDER BY ts DESC LIMIT {limit}", params, "search"

    aggs = q.get("aggs") or [{"fn": "count"}]
    increases = []   # per-point rises, computed before grouping (they need each series' previous point)
    group_params, groups = [], []
    for g in q.get("group_by") or []:
        groups.append(_time_bucket(g) or _derived(signal, g) or _field(signal, g, group_params))
    if any(a.get("fn") == "buckets" for a in aggs):
        if signal != "metrics" or len(aggs) != 1:
            raise BadQuery("buckets is for metrics, and the only aggregate of its query")
        if edges:
            return (f"SELECT {SERIES} AS s, min(ts) AS first_ts, max(ts) AS last_ts, "
                    + "".join(f"arg_min({g}, ts) AS g{j}, " for j, g in enumerate(groups))
                    + "arg_min(bucket_counts, ts) AS f0, arg_max(bucket_counts, ts) AS l0, "
                      "arg_min(explicit_bounds, ts) AS fb0, arg_max(explicit_bounds, ts) AS lb0, "
                      "arg_min(count, ts) AS fn0, arg_max(count, ts) AS ln0 "
                    f"FROM t WHERE {where_sql} AND temporality = {CUMULATIVE} AND bucket_counts IS NOT NULL GROUP BY 1",
                    group_params + params)
        return _buckets_sql(groups, where_sql), group_params + params, "aggregate"
    select, agg_params = [f"{g} AS g{i}" for i, g in enumerate(groups)], []
    for i, a in enumerate(aggs):
        fn = a.get("fn")
        if fn not in AGGS and not (isinstance(fn, str) and _PCT.fullmatch(fn) and 0 < float(fn[1:]) < 100):
            raise BadQuery(f"unknown aggregate {fn!r}; one of {sorted(AGGS)} or any percentile pNN")
        if fn == "count":
            select.append(f"count(*) AS a{i}")
            continue
        if fn == "increase":
            if signal != "metrics" or a.get("field") not in INCREASE_FIELDS:
                raise BadQuery(f"increase is for metrics, of one of {sorted(INCREASE_FIELDS)}")
            increases.append(f"{_increase(_field(signal, a['field'], []))} AS _inc{i}")
            select.append(f"sum(_inc{i}) AS a{i}")
            continue
        field_params = []
        x = _num(_field(signal, a.get("field"), field_params))
        if fn == "avg":
            parts = [f"sum({x}) AS a{i}_sum", f"count({x}) AS a{i}_n"]
        elif fn == "last":
            parts = [f"arg_max({x}, ts) AS a{i}", f"max(CASE WHEN {x} IS NOT NULL THEN ts END) AS a{i}_ts"]
        elif fn.startswith("p") or fn == "hist":
            # log-bucket histogram {bucket: count}; bucket -inf holds values <= 0
            parts = [f"histogram(CASE WHEN {x} > 0 THEN floor(ln({x}) / ln({HIST_BASE}))::INTEGER "
                     f"WHEN {x} IS NOT NULL THEN -2147483648 END) AS a{i}"]
        else:
            parts = [f"{fn}({x}) AS a{i}"]
        select += parts
        # The field appears several times; bind its parameters each time.
        agg_params += field_params * sum(p.count(x) for p in parts)
    if edges:
        if not increases:
            return None
        cols = [f"{SERIES} AS s", "min(ts) AS first_ts", "max(ts) AS last_ts"]
        cols += [f"arg_min({g}, ts) AS g{j}" for j, g in enumerate(groups)]
        for i, a in enumerate(aggs):
            if a.get("fn") == "increase":
                x = _num(_field(signal, a["field"], []))
                cols += [f"arg_min({x}, ts) AS f{i}", f"arg_max({x}, ts) AS l{i}"]
        return (f"SELECT {', '.join(cols)} FROM t WHERE {where_sql} AND temporality = {CUMULATIVE} GROUP BY 1",
                group_params + params)
    group_sql = f" GROUP BY {', '.join(str(i + 1) for i in range(len(groups)))}" if groups else ""
    source = f"t WHERE {where_sql}"
    if increases:
        source = f"(SELECT *, {', '.join(increases)} FROM t WHERE {where_sql})"
    sql = f"SELECT {', '.join(select)} FROM {source}{group_sql}"
    # Parameters in text order: SELECT (groups, then aggs), then WHERE.
    return sql, group_params + agg_params + params, "aggregate"


SERIES = "concat_ws('|', service, metric_name, attributes::VARCHAR, resource_attributes::VARCHAR)"


def _buckets_sql(groups, where_sql):
    """Metric histograms -> per group, {le: count} (a MAP) of measurements per bucket. A delta point
    counts as it is; a cumulative point counts its rise since the series' previous point, per
    bucket (a smaller total count, or other bounds, means it restarted: the whole point counts)."""
    g = ", ".join(f"g{i}" for i in range(len(groups)))
    gsel = "".join(f"{x} AS g{i}, " for i, x in enumerate(groups))
    return f"""
        WITH p AS (
            SELECT {gsel}temporality AS tmp, bucket_counts AS bc, explicit_bounds AS eb, count AS n,
                   lag(bucket_counts) OVER w AS pbc, lag(explicit_bounds) OVER w AS peb, lag(count) OVER w AS pn
            FROM t WHERE {where_sql} AND bucket_counts IS NOT NULL
            WINDOW w AS (PARTITION BY {SERIES} ORDER BY ts)),
        e AS (SELECT *, unnest(generate_series(1, len(bc))) AS i FROM p),
        r AS (
            SELECT {g + ", " if g else ""}
                   CASE WHEN i <= len(coalesce(eb, [])) THEN eb[i]::VARCHAR ELSE '+Inf' END AS le,
                   CASE WHEN tmp = {DELTA} THEN bc[i]
                        WHEN tmp = {CUMULATIVE} THEN CASE WHEN pbc IS NULL THEN NULL
                             WHEN n >= pn AND eb IS NOT DISTINCT FROM peb AND len(pbc) = len(bc) THEN bc[i] - pbc[i]
                             ELSE bc[i] END END AS c
            FROM e),
        s AS (SELECT {g + ", " if g else ""}le, sum(c) AS c FROM r WHERE c IS NOT NULL GROUP BY ALL)
        SELECT {g + ", " if g else ""}map(list(le), list(c)) AS a0 FROM s{" GROUP BY " + g if g else ""}"""


def _bucket_rise(first, last):
    """{le: rise} between a cumulative histogram's point `last` (bc, bounds, count) and the next
    point `first` (as _buckets_sql)."""
    (fb, fe, fn), (lb, le_, ln) = first, last
    if fb is None:
        return {}
    restarted = lb is None or fn is None or ln is None or fn < ln or fe != le_ or len(fb) != len(lb)
    out = {}
    for i, c in enumerate(fb):
        k = str(float(fe[i])) if fe and i < len(fe) else "+Inf"
        out[k] = c if restarted else c - lb[i]
    return out


def _row_cap(q):
    """Rows a query may return: MAX_ROWS, or more for PromQL's internal queries ("max_rows")."""
    return max(1, min(int(q.get("max_rows") or MAX_ROWS), MAX_INTERNAL_ROWS))


def _rise(x, prev):
    """A cumulative counter's rise from prev to x; a drop means it restarted from zero."""
    return x - prev if x >= prev else x


def _increase(x):
    """SQL: how much counter column x rose at each point (NULL for gauges and a series' first point)."""
    x = _num(x)
    prev = f"lag({x}) OVER (PARTITION BY {SERIES} ORDER BY ts)"
    return (f"CASE WHEN temporality = {DELTA} THEN {x} "
            f"WHEN temporality = {CUMULATIVE} THEN CASE WHEN {prev} IS NULL THEN NULL "
            f"WHEN {x} >= {prev} THEN {x} - {prev} ELSE {x} END END")


def _naive(ts):
    """ISO-8601 -> naive UTC timestamp text, as stored in Parquet."""
    return lookup._parse(ts).strftime("%Y-%m-%d %H:%M:%S.%f")


# -------------------------------------------------------------- worker

def worker(event, context):
    if "job" in event:
        return run_job(event)
    if "sql" in event:
        return run_sql_worker(event)
    return run_worker(event)


# ------------------------------------------------------------ SQL (read-only, one tenant)

SQL_TABLES = {"logs": "logs", "spans": "traces", "metrics": "metrics"}   # table -> signal
SQL_MAX_BYTES = int(os.environ.get("SQL_MAX_BYTES", str(1 << 30)))       # files one SQL query may read (29 s API limit)
SQL_MAX_ROWS = 10_000
SQL_MAX_FILES = 4_000
SQL_DOWNLOAD_THREADS = 64
SQL_MAX_LENGTH = 20_000


def check_sql(sql):
    """-> the tables one read-only SELECT uses. Refuses anything but a single SELECT over logs,
    spans and metrics (no table functions such as read_csv, no other tables)."""
    import duckdb
    if not isinstance(sql, str) or not sql.strip():
        raise BadQuery("sql must be a query")
    if len(sql) > SQL_MAX_LENGTH:
        raise BadQuery(f"the query is too long ({SQL_MAX_LENGTH:,} characters at most)")
    con = duckdb.connect()
    try:
        con.execute("SET enable_external_access=false")
        tree = json.loads(con.execute("SELECT json_serialize_sql(?)::VARCHAR", [sql]).fetchone()[0])
    finally:
        con.close()
    if tree.get("error"):
        msg = tree.get("error_message") or "not a query"
        raise BadQuery("only one SELECT query is allowed" if "Only SELECT" in msg else f"bad SQL: {msg}")
    if len(tree.get("statements") or []) != 1:
        raise BadQuery("give exactly one SELECT query")
    tables, ctes = set(), set()

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "TABLE_FUNCTION":
                raise BadQuery("table functions (read_csv, read_parquet, ...) are not allowed; query logs, spans or metrics")
            if n.get("type") == "BASE_TABLE":
                if n.get("schema_name") or n.get("catalog_name"):
                    raise BadQuery(f"unknown table {n.get('table_name')!r}; the tables are logs, spans and metrics")
                tables.add(n.get("table_name", "").lower())
            if isinstance(n.get("cte_map"), dict):
                for entry in n["cte_map"].get("map") or []:
                    ctes.add(str(entry.get("key", "")).lower())
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(tree["statements"][0])
    unknown = tables - set(SQL_TABLES) - ctes
    if unknown:
        raise BadQuery(f"unknown table {sorted(unknown)[0]!r}; the tables are logs, spans and metrics")
    used = sorted(tables & set(SQL_TABLES))
    if not used:
        raise BadQuery("query at least one of the tables logs, spans or metrics")
    return used


def run_sql(tenant, sql, start, end, invoke_worker=None):
    """One read-only SELECT over the tenant's logs / spans / metrics between start and end."""
    t0 = time.perf_counter()
    used = check_sql(sql)
    files = {}
    for table in used:
        found = lookup.lookup(tenant=tenant, start=start, end=end, signal=SQL_TABLES[table])
        files[table] = found["files"]
    size = sum(f.get("size_bytes", 0) for fs in files.values() for f in fs)
    count = sum(len(fs) for fs in files.values())
    if count > SQL_MAX_FILES:
        raise BadQuery(f"this time range has {count:,} files to read (at most {SQL_MAX_FILES:,}); choose a shorter time range")
    if size > SQL_MAX_BYTES:
        raise BadQuery(f"this would read {size / 2**30:.1f} GB (at most {SQL_MAX_BYTES / 2**30:.0f} GB); choose a shorter time range")
    event = {"sql": sql, "tenant": tenant, "start": start, "end": end, "tables": files}
    out = (invoke_worker or _invoke_worker)(event)
    out["stats"] = {**out.get("stats", {}), "files": sum(len(v) for v in files.values()), "total_ms": round((time.perf_counter() - t0) * 1000)}
    return out


def run_sql_worker(event):
    """Download the tenant's files, expose them as the views logs / spans / metrics (limited to
    the time range), then lock DuckDB down (no file, network or extension access; settings
    locked) and run the query."""
    t0 = time.perf_counter()
    tenant = layout.check_tenant(event["tenant"])
    check_sql(event["sql"])
    _, s3 = lookup._clients_for(tenant)
    work = tempfile.mkdtemp(dir="/tmp")
    prefix = f"s3://{lookup.BUCKET}/"
    try:
        allowed = []
        # Every file of every table, downloaded in parallel (an hour is hundreds of small fast-lane files).
        todo = []
        for table in SQL_TABLES:
            for f in sorted({f["file_path"]: f for f in (event["tables"].get(table) or [])}.values(), key=lambda f: f["file_path"]):
                todo.append((table, f, os.path.join(work, f"{len(todo):05d}{'.parquet' if _is_parquet(f) else '.json.gz'}")))
        with ThreadPoolExecutor(SQL_DOWNLOAD_THREADS) as pool:
            list(pool.map(lambda x: s3.download_file(lookup.BUCKET, x[1]["file_path"][len(prefix):], x[2]), todo))
        t_dl = time.perf_counter()
        con = compact._connect(work, f"{int(int(os.environ.get('AWS_LAMBDA_FUNCTION_MEMORY_SIZE', '2048')) * 0.6)}MB")
        try:
            for table, signal in SQL_TABLES.items():
                mine = [(f, local) for t, f, local in todo if t == table]
                pq = [local for f, local in mine if _is_parquet(f)]
                parts = []
                if pq:
                    allowed += pq
                    paths = ", ".join("'" + p.replace("'", "''") + "'" for p in pq)
                    parts.append(f"SELECT * FROM read_parquet([{paths}], union_by_name = true, hive_partitioning = false)")
                for i, (f, local) in enumerate((f, l) for f, l in mine if not _is_parquet(f)):
                    parsed = layout.parse_incoming_key(f["file_path"][len(prefix):])
                    compact.load_rows(con, [local], parsed[2], parsed[3], signal)
                    con.execute(f"CREATE TEMP TABLE raw_{table}_{i} AS SELECT * FROM rows")
                    parts.append(f"SELECT * FROM raw_{table}_{i}")
                if not parts:   # no data: an empty table with the signal's columns
                    empty = os.path.join(work, f"empty-{table}.json")
                    with open(empty, "w") as fh:
                        json.dump({"logs": {"resourceLogs": []}, "traces": {"resourceSpans": []},
                                   "metrics": {"resourceMetrics": []}}[signal], fh)
                    compact.load_rows(con, [empty], "2000-01-01", "00", signal)
                    con.execute(f"CREATE TEMP TABLE empty_{table} AS SELECT * FROM rows")
                    parts.append(f"SELECT * FROM empty_{table}")
                con.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM ({' UNION ALL BY NAME '.join(parts)}) "
                            f"WHERE ts >= '{_naive(event['start'])}'::TIMESTAMP AND ts < '{_naive(event['end'])}'::TIMESTAMP")
            con.execute("DROP TABLE IF EXISTS rows")
            if allowed:
                con.execute("SET allowed_paths=[" + ", ".join("'" + p.replace("'", "''") + "'" for p in allowed) + "]")
            for setting in ("enable_external_access=false", "autoinstall_known_extensions=false",
                            "autoload_known_extensions=false", "lock_configuration=true"):
                con.execute(f"SET {setting}")
            t_load = time.perf_counter()
            try:
                cur = con.execute(event["sql"])
            except duckdb_error() as e:
                return {"error": str(e).split("\n")[0][:500]}
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(SQL_MAX_ROWS + 1)
        finally:
            con.close()
        return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:SQL_MAX_ROWS]],
                "truncated": len(rows) > SQL_MAX_ROWS,
                "stats": {"download_ms": round((t_dl - t0) * 1000), "load_ms": round((t_load - t_dl) * 1000), "query_ms": round((time.perf_counter() - t_load) * 1000)}}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def duckdb_error():
    import duckdb
    return duckdb.Error


def run_worker(event):
    """Read this chunk's files as the tenant, load them, run the query.

    Parquet is read in place ("ranges", the default): DuckDB asks for the
    footer and just the column chunks the query needs, fetched as S3 range
    requests. "download" fetches whole files first (the Phase 4 baseline).
    Fast-lane entries are Parquet too; only entries indexed before the fast
    lane wrote Parquet point at raw JSON, which is downloaded and parsed."""
    t0 = time.perf_counter()
    q = event["query"]
    tenant = layout.check_tenant(q["tenant"])
    signal = q.get("signal", "logs")
    sql, params, kind = compile_query(q)
    mode = q.get("read", READ_MODE)
    _, s3 = lookup._clients_for(tenant)
    work = tempfile.mkdtemp(dir="/tmp")
    try:
        files = sorted({f["file_path"]: f for f in event["files"]}.values(), key=lambda f: f["file_path"])
        prefix = f"s3://{lookup.BUCKET}/"
        in_place = [f for f in files if _is_parquet(f) and mode == "ranges"]

        def fetch(i_f):
            i, f = i_f
            ext = ".parquet" if _is_parquet(f) else ".json.gz"
            local = os.path.join(work, f"{i:05d}{ext}")
            s3.download_file(lookup.BUCKET, f["file_path"][len(prefix):], local)
            return f, local
        with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
            local = list(pool.map(fetch, [(i, f) for i, f in enumerate(files) if f not in in_place]))
        t_dl = time.perf_counter()
        con = compact._connect(work, f"{int(int(os.environ.get('AWS_LAMBDA_FUNCTION_MEMORY_SIZE', '2048')) * 0.6)}MB")
        ranges = TenantS3(s3, lookup.BUCKET, {f["file_path"][len(prefix):]: f["size_bytes"] for f in in_place})
        try:
            parts = []
            pq = [p for f, p in local if _is_parquet(f)]
            if in_place:
                con.register_filesystem(ranges)
                # Reading in place mostly waits on S3: more threads keep more
                # range requests in flight than one per vCPU would.
                con.execute(f"SET threads = {RANGE_THREADS}")
                pq += [f"{TenantS3.protocol}://{f['file_path'][len(prefix):]}" for f in in_place]
            if pq:
                # Our own local paths (views can't take parameters).
                paths = ", ".join("'" + p.replace("'", "''") + "'" for p in pq)
                # hive_partitioning off: the dt=/hour=/service= folders in S3 paths must
                # not become extra columns (the files hold the real ones).
                con.execute(f"CREATE TEMP VIEW pq AS SELECT * FROM read_parquet([{paths}], union_by_name = true, "
                            "hive_partitioning = false)")
                parts.append("SELECT * FROM pq")
            raw = [(f, p) for f, p in local if not _is_parquet(f)]
            for i, (f, p) in enumerate(raw):
                # Raw (not yet compacted) files: parsed exactly as compaction would.
                parsed = layout.parse_incoming_key(f["file_path"][len(prefix):])
                compact.load_rows(con, [p], parsed[2], parsed[3], signal)
                con.execute(f"CREATE TEMP TABLE raw{i} AS SELECT * FROM rows")
                parts.append(f"SELECT * FROM raw{i}")
            if not parts:
                return {"kind": kind, "rows": [], "columns": [], "stats": {"files": 0}}
            con.execute("CREATE TEMP VIEW t AS " + " UNION ALL BY NAME ".join(parts))
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            cap = _row_cap(q)
            rows = cur.fetchmany(cap + 1)
            edges = compile_query(q, edges=True) if kind == "aggregate" else None
            if edges:
                cur = con.execute(*edges)
                edges = {"columns": [d[0] for d in cur.description],
                         "rows": [[_jsonable(v) for v in r] for r in cur.fetchall()]}
            scanned = con.execute("SELECT count(*) FROM t").fetchone()[0] if event.get("count_scanned") else None
        finally:
            con.close()
        return {"kind": kind, "columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:cap]],
                "truncated": len(rows) > cap, **({"edges": edges} if edges else {}),
                "stats": {"files": len(files), "read_mode": mode,
                          "bytes": sum(os.path.getsize(p) for _, p in local) + ranges.bytes_read,
                          "range_requests": ranges.requests,
                          "download_ms": round((t_dl - t0) * 1000), "query_ms": round((time.perf_counter() - t_dl) * 1000),
                          "rows_scanned": scanned}}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _is_parquet(f):
    return f["file_path"].endswith(".parquet")


class _RangeFile(AbstractBufferedFile):
    def _fetch_range(self, start, end):
        body = self.fs.s3.get_object(Bucket=self.fs.bucket, Key=self.path, Range=f"bytes={start}-{end - 1}")["Body"].read()
        with self.fs.lock:
            self.fs.requests += 1
            self.fs.bytes_read += len(body)
        return body


class TenantS3(AbstractFileSystem):
    """Lets DuckDB read S3 objects in place, as byte ranges, through a
    tenant-scoped client (so IAM still refuses other tenants' objects). File
    sizes come from the index, so no HEAD request per file."""
    protocol = "obsq"

    def __init__(self, s3, bucket, sizes, **kw):
        super().__init__(skip_instance_cache=True, **kw)
        self.s3, self.bucket, self.sizes = s3, bucket, sizes
        self.lock = threading.Lock()
        self.requests = self.bytes_read = 0

    def info(self, path, **kw):
        path = self._strip_protocol(path)
        return {"name": path, "size": self.sizes[path], "type": "file"}

    def modified(self, path):
        return datetime(2000, 1, 1)   # files are immutable once written

    def _open(self, path, mode="rb", block_size=None, **kw):
        path = self._strip_protocol(path)
        return _RangeFile(self, path, mode, block_size=block_size or RANGE_BLOCK_BYTES,
                          cache_type="readahead", size=self.sizes[path])


def _jsonable(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(v, (date, dt_time, Decimal)):
        return str(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# --------------------------------------------------------- coordinator

def handler(event, context):
    """Direct invocations (other Leasyd functions: alerts, the AI SRE), for the event's tenant:
      a JSON query;
      {"tenant", "promql", "time"}: one PromQL evaluation (the Prometheus API's vector result);
      {"tenant", "promql", "start", "end", "step"}: a PromQL range (matrix);
      {"tenant", "sql", "start", "end"}: read-only SQL."""
    try:
        if "promql" in event:
            import promql
            tenant = layout.check_tenant(event["tenant"])
            kept = int(kept_from().timestamp())
            if event.get("start") is not None:
                return promql.query_range(tenant, event["promql"], max(_epoch_of(event["start"], "start"), kept),
                                          max(_epoch_of(event["end"], "end"), kept), int(float(event.get("step") or 60)))
            return promql.query_instant(tenant, event["promql"], _epoch_of(event.get("time") or time.time(), "time"))
        if "sql" in event:
            tenant = layout.check_tenant(event["tenant"])
            start, end = (max(datetime.fromtimestamp(_epoch_of(event[k], k), timezone.utc), kept_from()).strftime("%Y-%m-%dT%H:%M:%SZ")
                          for k in ("start", "end"))
            return run_sql(tenant, event["sql"], start, end)
        return run(event)
    except BadQuery as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": f"bad query: {e}"}


# ------------------------------------------------------------ HTTP API

API_FIELDS = {"signal", "start", "end", "services", "where", "match", "group_by", "aggs", "search", "order", "limit", "collapse"}
MAX_RESPONSE_BYTES = 5_500_000   # Lambda's response limit is 6 MB


SYNC_DEADLINE_S = 20    # a query still running then continues as a job (API Gateway gives up at 29 s)
JOB_TIMEOUT_S = 330     # a job not done by then has failed (the worker function's timeout is 300 s)
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def api(event, context):
    """Queries through API Gateway:
      POST /v1/query            read key (API key authorizer)
      POST /v1/app/query        signed-in user (Cognito authorizer, ID token)
      GET  /v1/query/{job}, /v1/app/query/{job}   a long query's answer (see below)
      GET  /v1/app/me           who the signed-in user is
    A query that takes longer than SYNC_DEADLINE_S, or asks for it ("async": true), runs on as a
    job: the answer is 202 {"job", "status": "running"}, and GET .../query/{job} answers 202
    while it runs, then the query's own answer (kept a day). Jobs run in obs-query-worker
    (up to 5 minutes) and their answers are stored under _results/jobs/<tenant>/.
    The tenant comes only from the authorizer: the key's tenant, or the
    user's custom:tenant claim (set by the tenant admin, not changeable by
    the user). A tenant or any other field in the body outside API_FIELDS is
    ignored. Unexpected errors raise, so they count as Lambda errors
    (alarmed) and the caller gets a 5xx."""
    auth = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = auth.get("claims") or {}
    tenant = auth.get("tenant") or claims.get("custom:tenant")
    if not tenant or not layout._TENANT.match(tenant):
        return _http(401, {"error": "no tenant for this key or user"})
    if event.get("resource") == "/v1/app/me":
        return _http(200, {"tenant": tenant, "email": claims.get("email")})
    if event.get("httpMethod") == "GET":
        return job_status(tenant, ((event.get("pathParameters") or {}).get("job") or ""))
    if (refused := over_limit(tenant, "query")):
        return refused
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body)
    try:
        q = json.loads(body)
    except ValueError:
        return _http(400, {"error": "body must be a JSON query"})
    if not isinstance(q, dict):
        return _http(400, {"error": "body must be a JSON object"})
    if q.get("async"):
        return start_job(tenant, q)
    # Run here; past the deadline, hand the query to a job (it starts again there) and say so.
    pool = ThreadPoolExecutor(1)
    running = pool.submit(answer, tenant, q)
    pool.shutdown(wait=False)
    try:
        return running.result(timeout=SYNC_DEADLINE_S)
    except FutureTimeout:
        return start_job(tenant, q)


def answer(tenant, q):
    """A query (JSON, PromQL or SQL) for one tenant -> the HTTP answer."""
    if "promql" in q:
        return _promql_api(tenant, q)
    if "sql" in q:
        return _sql_api(tenant, q)
    q = {k: v for k, v in q.items() if k in API_FIELDS}
    if not q.get("start") or not q.get("end"):
        return _http(400, {"error": "start and end are required (ISO-8601, e.g. 2026-09-28T00:00:00Z)"})
    q["tenant"] = tenant
    try:
        kept = kept_from()
        if lookup._parse(q["start"]) < kept:
            q["start"] = kept.strftime("%Y-%m-%dT%H:%M:%SZ")
            if lookup._parse(q["end"]) < kept:
                q["end"] = q["start"]          # entirely before what is kept: an empty answer
        out = run(q)
    except BadQuery as e:
        return _http(400, {"error": str(e)})
    except ValueError as e:     # e.g. an unparseable timestamp
        return _http(400, {"error": f"bad query: {e}"})
    text = json.dumps(out)
    if len(text) > MAX_RESPONSE_BYTES:
        return _http(413, {"error": "result too large; ask for fewer rows (search.limit / limit)"})
    return _http(200, text)


def _job_key(tenant, job):
    return f"_results/jobs/{tenant}/{job}.json"


def start_job(tenant, q):
    """Run the query in the background (obs-query-worker, async) -> 202 with the job's id."""
    if (refused := over_limit(tenant, "job")):
        return refused
    job = secrets.token_hex(16)
    _s3().put_object(Bucket=lookup.BUCKET, Key=_job_key(tenant, job), ContentType="application/json",
                     Body=json.dumps({"status": "running", "started": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}))
    _run_in_background({"job": {"id": job, "tenant": tenant, "query": {k: v for k, v in q.items() if k != "async"}}})
    return _http(202, {"job": job, "status": "running",
                       "message": "the query runs in the background; GET /v1/app/query/{job} (or /v1/query/{job}) for its answer"})


# Per tenant and minute, by plan: queries, and background jobs (each up to 5 minutes of a worker).
# One customer can't use up the account's Lambda capacity (shared with everyone's ingest) or run
# up the bill. Counted in obs-tenants (rate#query#<tenant>#<minute>, expiring with its TTL).
RATE_LIMITS = {"query": {"free": 120, "standard": 600}, "job": {"free": 4, "standard": 20}}
_plans = {}   # tenant -> (plan, read at)


def _plan(table, tenant):
    plan, at = _plans.get(tenant, (None, 0))
    if time.time() - at > 300:
        item = table.get_item(Key={"pk": f"tenant#{tenant}"}).get("Item") or {}
        plan = item.get("plan") or "standard"
        _plans[tenant] = (plan, time.time())
    return plan


def over_limit(tenant, kind):
    """None, or the 429 answer when the tenant is over this minute's limit. Fails open (logged):
    a counting problem must not stop customers' queries."""
    try:
        table = boto3.resource("dynamodb").Table(os.environ.get("TENANTS_TABLE", "obs-tenants"))
        limit = RATE_LIMITS[kind].get(_plan(table, tenant), RATE_LIMITS[kind]["standard"])
        minute = int(time.time() // 60)
        table.update_item(Key={"pk": f"rate#{kind}#{tenant}#{minute}"}, UpdateExpression="ADD n :one SET expires = :exp",
                          ConditionExpression="attribute_not_exists(n) OR n < :max",
                          ExpressionAttributeValues={":one": 1, ":max": limit, ":exp": minute * 60 + 3600})
        return None
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            what = "queries" if kind == "query" else "long-running queries"
            return _http(429, {"error": f"too many {what}: at most {limit} a minute on this plan; try again in a minute"})
        print(json.dumps({"rate_limit_unavailable": str(e)[:300]}))
        return None


def _run_in_background(payload):
    boto3.client("lambda").invoke(FunctionName=WORKER_FUNCTION, InvocationType="Event", Payload=json.dumps(payload))


def run_job(event):
    """In obs-query-worker: run a job's query and store its answer for job_status."""
    job = event["job"]
    tenant = layout.check_tenant(job["tenant"])
    if not _JOB_ID.match(job["id"]):
        raise ValueError("bad job id")
    try:
        out = answer(tenant, job["query"])
    except Exception:
        out = _http(500, {"error": "the query failed; try a shorter time range"})
        _s3().put_object(Bucket=lookup.BUCKET, Key=_job_key(tenant, job["id"]), ContentType="application/json",
                         Body=json.dumps({"status": "done", "answer": out}))
        raise
    _s3().put_object(Bucket=lookup.BUCKET, Key=_job_key(tenant, job["id"]), ContentType="application/json",
                     Body=json.dumps({"status": "done", "answer": out}))
    return {"job": job["id"], "statusCode": out["statusCode"]}


def job_status(tenant, job):
    """GET .../query/{job} -> 202 while it runs, then the query's own answer."""
    if not _JOB_ID.match(job):
        return _http(404, {"error": "no such query job"})
    try:
        state = json.loads(_s3().get_object(Bucket=lookup.BUCKET, Key=_job_key(tenant, job))["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404", "AccessDenied"):
            return _http(404, {"error": "no such query job (answers are kept a day)"})
        raise
    if state["status"] == "done":
        return state["answer"]
    started = lookup._parse(state["started"])
    if (_now() - started).total_seconds() > JOB_TIMEOUT_S:
        return _http(504, {"error": "the query took longer than 5 minutes; try a shorter time range or aggregate more"})
    return _http(202, {"job": job, "status": "running", "started": state["started"]})


def _s3():
    return boto3.client("s3")


def _epoch_of(v, name):
    """ISO-8601 or epoch seconds -> epoch seconds."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, str) and v:
        try:
            return int(float(v))
        except ValueError:
            return int(lookup._parse(v).timestamp())
    raise BadQuery(f"{name} is required (ISO-8601 or epoch seconds)")


def _promql_api(tenant, q):
    """{"promql": "...", "start", "end", "step"} -> a range query; {"promql", "time"} -> an instant query.
    Answers in the Prometheus HTTP API's format."""
    import promql
    text = q.get("promql")
    if not isinstance(text, str) or not text.strip():
        return _http(400, {"status": "error", "errorType": "bad_data", "error": "promql must be a query"})
    try:
        kept = int(kept_from().timestamp())
        if q.get("time") is not None and q.get("start") is None:
            out = promql.query_instant(tenant, text, max(_epoch_of(q["time"], "time"), kept))
        else:
            start, end = _epoch_of(q.get("start"), "start"), _epoch_of(q.get("end"), "end")
            step = int(float(q.get("step") or 60))
            out = promql.query_range(tenant, text, max(start, kept), max(end, kept), step)
    except BadQuery as e:
        return _http(400, {"status": "error", "errorType": "bad_data", "error": str(e)})
    except ValueError as e:
        return _http(400, {"status": "error", "errorType": "bad_data", "error": f"bad query: {e}"})
    text = json.dumps(out)
    if len(text) > MAX_RESPONSE_BYTES:
        return _http(413, {"status": "error", "errorType": "too_large", "error": "result too large; aggregate or use a larger step"})
    return _http(200, text)


def _sql_api(tenant, q):
    """{"sql": "SELECT ...", "start", "end"} -> {"columns", "rows", "truncated", "stats"}."""
    if not q.get("start") or not q.get("end"):
        return _http(400, {"error": "start and end are required (ISO-8601 or epoch seconds)"})
    try:
        kept = kept_from()
        start, end = (max(datetime.fromtimestamp(_epoch_of(q[k], k), timezone.utc), kept).strftime("%Y-%m-%dT%H:%M:%SZ")
                      for k in ("start", "end"))
        out = run_sql(tenant, q["sql"], start, end)
    except BadQuery as e:
        return _http(400, {"error": str(e)})
    except ValueError as e:
        return _http(400, {"error": f"bad query: {e}"})
    if out.get("error"):
        return _http(400, {"error": out["error"]})
    text = json.dumps(out)
    if len(text) > MAX_RESPONSE_BYTES:
        return _http(413, {"error": "result too large; select fewer columns or add LIMIT"})
    return _http(200, text)


def _now():
    return datetime.now(timezone.utc)


def kept_from():
    """The oldest moment kept: midnight UTC, RETENTION_DAYS days before today (as the tenant
    admin's retention_cutoff)."""
    return datetime.combine((_now() - timedelta(days=RETENTION_DAYS)).date(), dt_time(0), tzinfo=timezone.utc)


def _http(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": body if isinstance(body, str) else json.dumps(body), "isBase64Encoded": False}


def run(q, invoke_worker=None):
    t0 = time.perf_counter()
    compile_query(q)  # reject bad queries before any work
    found = lookup.lookup(tenant=q["tenant"], start=q["start"], end=q["end"], signal=q.get("signal", "logs"),
                          services=q.get("services"), match=q.get("match"))
    t_lookup = time.perf_counter()
    # A counter's rise is taken between consecutive points, so each worker needs an unbroken
    # stretch of time: otherwise the rise over a stretch another worker holds is counted twice.
    contiguous = any(a.get("fn") in ("increase", "buckets") for a in q.get("aggs") or [])
    chunks = plan_chunks(found["files"], int(q.get("workers", MAX_WORKERS)), contiguous)
    invoke_worker = invoke_worker or _invoke_worker
    if len(chunks) <= 1:
        partials = [run_worker({"query": q, "files": chunks[0] if chunks else []})]
    else:
        with ThreadPoolExecutor(len(chunks)) as pool:
            partials = list(pool.map(lambda c: invoke_worker({"query": q, "files": c}), chunks))
    t_scan = time.perf_counter()
    result = merge(q, partials)
    result["stats"] = {
        "files": len(found["files"]), "workers": max(1, len(chunks)),
        "bytes": sum(p.get("stats", {}).get("bytes", 0) for p in partials),
        "lookup_ms": round((t_lookup - t0) * 1000), "scan_ms": round((t_scan - t_lookup) * 1000),
        "merge_ms": round((time.perf_counter() - t_scan) * 1000), "total_ms": round((time.perf_counter() - t0) * 1000),
        "slowest_worker_download_ms": max((p.get("stats", {}).get("download_ms", 0) for p in partials), default=0),
        "slowest_worker_query_ms": max((p.get("stats", {}).get("query_ms", 0) for p in partials), default=0),
        "lookup": found["stats"],
    }
    return result


def plan_chunks(files, max_workers, contiguous=False):
    """Split files into <= max_workers chunks of similar total cost (largest first); a file costs
    its size plus FILE_COST_BYTES. contiguous: each chunk is instead a run of files in time order
    (by min_ts), cut at about equal costs, and only where no earlier file reaches past the next file's
    start: chunks then cover separate stretches of time (files of one series can overlap, e.g. when
    compaction batches hold interleaved minutes; a cut inside an overlap would count a rise twice)."""
    cost = lambda f: f["size_bytes"] + FILE_COST_BYTES   # noqa: E731
    files = sorted({f["file_path"]: f for f in files}.values(), key=lambda f: -cost(f))
    if not files:
        return []
    total = sum(cost(f) for f in files)
    n = max(1, min(max_workers, MAX_WORKERS, len(files), math.ceil(total / TARGET_BYTES_PER_WORKER)))
    if contiguous:
        chunks, size, reach = [[]], 0, None   # reach: the latest max_ts so far
        for f in sorted(files, key=lambda f: (lookup._parse(f["min_ts"]), f["file_path"])):
            start, end = lookup._parse(f["min_ts"]), lookup._parse(f.get("max_ts") or f["min_ts"])
            if chunks[-1] and size >= total * len(chunks) / n and len(chunks) < n and start > reach:
                chunks.append([])
            chunks[-1].append(f)
            size += cost(f)
            reach = end if reach is None else max(reach, end)
        return chunks
    bins = [[0, []] for _ in range(n)]
    for f in files:
        b = min(bins, key=lambda x: x[0])
        b[0] += cost(f)
        b[1].append(f)
    return [b[1] for b in bins if b[1]]


def _invoke_worker(payload, attempts=2):
    """Run one worker. Workers only read, so a worker whose process died (DuckDB has crashed, rarely,
    in a fresh container: about 1 in 3,000 runs) is run once more instead of failing the query."""
    for attempt in range(attempts):
        r = lam.invoke(FunctionName=WORKER_FUNCTION, Payload=json.dumps(payload).encode())
        out = json.loads(r["Payload"].read())
        if not r.get("FunctionError"):
            return out
        crashed = isinstance(out, dict) and out.get("errorType") == "Runtime.ExitError"
        print(json.dumps({"worker_failed": out.get("errorType") if isinstance(out, dict) else str(out)[:200],
                          "attempt": attempt + 1, "retrying": crashed and attempt + 1 < attempts}))
        if not crashed:
            break
    raise RuntimeError(f"query worker failed: {out}")


def merge(q, partials):
    """Combine the workers' partial results."""
    if q.get("search") is not None:
        limit = min(int((q.get("search") or {}).get("limit", 100)), MAX_ROWS)
        cols = next((p["columns"] for p in partials if p.get("columns")), [])
        rows = []
        for p in partials:
            idx = {c: i for i, c in enumerate(p.get("columns", []))}
            rows += [[r[idx[c]] if c in idx else None for c in cols] for r in p.get("rows", [])]
        ts = cols.index("ts") if "ts" in cols else None
        if ts is not None:
            rows.sort(key=lambda r: r[ts] or "", reverse=True)
        return {"columns": cols, "rows": rows[:limit]}

    groups = q.get("group_by") or []
    aggs = q.get("aggs") or [{"fn": "count"}]
    merged = {}
    for p in partials:
        idx = {c: i for i, c in enumerate(p.get("columns", []))}
        for r in p.get("rows", []):
            key = tuple(r[idx[f"g{i}"]] for i in range(len(groups)))
            acc = merged.setdefault(key, [None] * len(aggs))
            for i, a in enumerate(aggs):
                fn = a["fn"]
                if fn == "avg":
                    s, n = r[idx[f"a{i}_sum"]], r[idx[f"a{i}_n"]]
                    old = acc[i] or (0.0, 0)
                    acc[i] = (old[0] + (s or 0), old[1] + (n or 0))
                elif fn == "last":
                    v, t = r[idx[f"a{i}"]], r[idx[f"a{i}_ts"]]
                    if t is not None and (acc[i] is None or t > acc[i][1]):
                        acc[i] = (v, t)
                elif fn == "buckets":
                    old = acc[i] or {}
                    for b, c in (r[idx[f"a{i}"]] or {}).items():
                        old[_le(b)] = old.get(_le(b), 0) + (c or 0)
                    acc[i] = old
                elif fn.startswith("p") or fn == "hist":
                    h = r[idx[f"a{i}"]] or {}
                    old = acc[i] or {}
                    for b, c in h.items():
                        old[int(b)] = old.get(int(b), 0) + c
                    acc[i] = old
                else:
                    v = r[idx[f"a{i}"]]
                    if v is None:
                        continue
                    if acc[i] is None:
                        acc[i] = v
                    elif fn in ("count", "sum", "increase"):
                        acc[i] += v
                    elif fn == "min":
                        acc[i] = min(acc[i], v)
                    elif fn == "max":
                        acc[i] = max(acc[i], v)
    _stitch(groups, aggs, partials, merged)
    rows = []
    for key, acc in merged.items():
        out = list(key)
        for a, v in zip(aggs, acc):
            if a["fn"] == "avg":
                out.append(v[0] / v[1] if v and v[1] else None)
            elif a["fn"] == "last":
                out.append(v[0] if v else None)
            elif a["fn"] == "hist":
                out.append({str(b): c for b, c in sorted((v or {}).items())})
            elif a["fn"] == "buckets":
                out.append({b: c for b, c in sorted((v or {}).items(), key=lambda x: float(x[0]))})
            elif a["fn"].startswith("p"):
                out.append(_percentile(v or {}, float(a["fn"][1:]) / 100))
            else:
                out.append(v if v is not None else (0 if a["fn"] == "count" else None))
        rows.append(out)
    collapse = q.get("collapse")
    if collapse is not None:
        # Count the groups under each of the first `collapse` group columns (e.g. series per metric,
        # with group_by [metric_name, "hash:attributes"]), summing counts and sums.
        n = int(collapse)
        if not 0 <= n < len(groups) or any(a["fn"] not in ("count", "sum") for a in aggs):
            raise BadQuery("collapse: fewer columns than group_by, with count or sum aggregates only")
        folded = {}
        for r in rows:
            acc = folded.setdefault(tuple(r[:n]), [0] + [0] * len(aggs))
            acc[0] += 1
            for j in range(len(aggs)):
                acc[1 + j] += r[len(groups) + j] or 0
        rows = [list(k) + v for k, v in folded.items()]
        groups = list(groups[:n]) + ["groups"]
    i = len(groups)   # order by the first aggregate; empty values last either way
    if q.get("order", "desc") == "desc":
        rows.sort(key=lambda r: (r[i] is not None, _sortable(r[i])), reverse=True)
    else:
        rows.sort(key=lambda r: (r[i] is None, _sortable(r[i])))
    cols = list(groups) + [a["fn"] if a["fn"] == "count" else f"{a['fn']}({a.get('field')})" for a in aggs]
    out = {"columns": cols, "rows": rows[:min(int(q.get("limit", 100)), _row_cap(q))]}
    if any(p.get("truncated") for p in partials):
        out["truncated"] = True     # a worker had more groups than MAX_ROWS: counts (and collapse) are partial
    return out


def _sortable(v):
    return v if isinstance(v, (int, float)) else 0   # histograms ("hist") don't order results


def _stitch(groups, aggs, partials, merged):
    """Counters: add each series' rise between consecutive workers (partials are in time order,
    see plan_chunks), to the group of the later worker's first point."""
    if aggs and aggs[0]["fn"] == "buckets":
        return _stitch_buckets(groups, partials, merged)
    inc = [i for i, a in enumerate(aggs) if a["fn"] == "increase"]
    last = {}   # series -> (ts, {agg index: value}) of its latest point so far
    for p in partials:
        e = p.get("edges")
        if not e:
            continue
        idx = {c: i for i, c in enumerate(e["columns"])}
        for r in e["rows"]:
            s = r[idx["s"]]
            if s in last and r[idx["first_ts"]] > last[s][0]:
                acc = merged.setdefault(tuple(r[idx[f"g{j}"]] for j in range(len(groups))), [None] * len(aggs))
                for i in inc:
                    x, prev = r[idx[f"f{i}"]], last[s][1][i]
                    if x is not None and prev is not None:
                        acc[i] = (acc[i] or 0) + _rise(x, prev)
            if s not in last or r[idx["last_ts"]] > last[s][0]:
                last[s] = (r[idx["last_ts"]], {i: r[idx[f"l{i}"]] for i in inc})


def _stitch_buckets(groups, partials, merged):
    """_stitch for a buckets query: each cumulative histogram's rise between consecutive workers."""
    last = {}   # series -> (ts, (bucket counts, bounds, count)) of its latest point so far
    for p in partials:
        e = p.get("edges")
        if not e:
            continue
        idx = {c: i for i, c in enumerate(e["columns"])}
        for r in e["rows"]:
            s = r[idx["s"]]
            if s in last and r[idx["first_ts"]] > last[s][0]:
                acc = merged.setdefault(tuple(r[idx[f"g{j}"]] for j in range(len(groups))), [None])
                h = acc[0] or {}
                for k, c in _bucket_rise((r[idx["f0"]], r[idx["fb0"]], r[idx["fn0"]]), last[s][1]).items():
                    h[_le(k)] = h.get(_le(k), 0) + c
                acc[0] = h
            if s not in last or r[idx["last_ts"]] > last[s][0]:
                last[s] = (r[idx["last_ts"]], (r[idx["l0"]], r[idx["lb0"]], r[idx["ln0"]]))


def _le(b):
    """A bucket's upper bound as one spelling ("5.0", "+Inf"), whichever way it was written."""
    f = float(b)
    return "+Inf" if f == math.inf else str(f)


def _percentile(hist, p):
    total = sum(hist.values())
    if not total:
        return None
    target = p * total
    seen = 0
    for b in sorted(hist):
        seen += hist[b]
        if seen >= target:
            if b == -2147483648:
                return 0.0
            return HIST_BASE ** (b + 0.5)   # bucket midpoint (geometric)
    return None
