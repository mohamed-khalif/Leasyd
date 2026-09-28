"""Turn raw OTLP JSON batch files into sorted Parquet, one file per
(service, event hour). Pure local-file logic, no AWS calls, so it can be
tested without S3.

One row per item, by signal:
  logs     one per log record, at its timestamp
  traces   one per span, at its start time; events and links nested
  metrics  one per data point, at its timestamp; gauge, sum, histogram,
           exponential histogram and summary points share one table,
           with metric_type saying which columns are set
"""

import gzip
import os
import re
import shutil

import duckdb

import bloom
import dayfilter

# Largest single batch file (uncompressed). The collector caps batches at
# 40k records, ~15 MB; DuckDB allocates a buffer this size per thread.
MAX_JSON_OBJECT_BYTES = 64 * 1024 * 1024

_ATTR = "STRUCT(key VARCHAR, value JSON)[]"
LOGS_JSON_TYPE = (
    "STRUCT("
    f"resource STRUCT(attributes {_ATTR}), "
    "scopeLogs STRUCT("
    "scope STRUCT(name VARCHAR, version VARCHAR), "
    "logRecords STRUCT("
    "timeUnixNano VARCHAR, observedTimeUnixNano VARCHAR, "
    "severityNumber INTEGER, severityText VARCHAR, body JSON, "
    f"attributes {_ATTR}, traceId VARCHAR, spanId VARCHAR"
    ")[]"
    ")[]"
    ")[]"
)

SPANS_JSON_TYPE = (
    "STRUCT("
    f"resource STRUCT(attributes {_ATTR}), "
    "scopeSpans STRUCT("
    "scope STRUCT(name VARCHAR, version VARCHAR), "
    "spans STRUCT("
    "traceId VARCHAR, spanId VARCHAR, parentSpanId VARCHAR, traceState VARCHAR, "
    "name VARCHAR, kind VARCHAR, startTimeUnixNano VARCHAR, endTimeUnixNano VARCHAR, "
    f"attributes {_ATTR}, "
    f"events STRUCT(timeUnixNano VARCHAR, name VARCHAR, attributes {_ATTR})[], "
    f"links STRUCT(traceId VARCHAR, spanId VARCHAR, traceState VARCHAR, attributes {_ATTR})[], "
    "status STRUCT(message VARCHAR, code VARCHAR)"
    ")[]"
    ")[]"
    ")[]"
)

# Numbers are read as VARCHAR (int64s arrive as JSON strings) or JSON
# (doubles may be numbers or "NaN" / "Infinity" strings), then cast.
_DP = f"attributes {_ATTR}, startTimeUnixNano VARCHAR, timeUnixNano VARCHAR, flags VARCHAR"
_STATS = '"count" VARCHAR, "sum" JSON, "min" JSON, "max" JSON'  # quoted: SQL keywords
_NUMBER_DP = f"STRUCT({_DP}, asDouble JSON, asInt VARCHAR)[]"
_HIST_DP = f"STRUCT({_DP}, {_STATS}, bucketCounts VARCHAR[], explicitBounds JSON[])[]"
_BUCKETS = 'STRUCT("offset" INTEGER, bucketCounts VARCHAR[])'
_EXP_DP = f"STRUCT({_DP}, {_STATS}, scale INTEGER, zeroCount VARCHAR, positive {_BUCKETS}, negative {_BUCKETS})[]"
_SUMMARY_DP = f'STRUCT({_DP}, "count" VARCHAR, "sum" JSON, quantileValues STRUCT(quantile JSON, value JSON)[])[]'
METRICS_JSON_TYPE = (
    "STRUCT("
    f"resource STRUCT(attributes {_ATTR}), "
    "scopeMetrics STRUCT("
    "scope STRUCT(name VARCHAR, version VARCHAR), "
    "metrics STRUCT("
    "name VARCHAR, description VARCHAR, unit VARCHAR, "
    f"gauge STRUCT(dataPoints {_NUMBER_DP}), "
    f'"sum" STRUCT(dataPoints {_NUMBER_DP}, aggregationTemporality VARCHAR, isMonotonic BOOLEAN), '
    f"histogram STRUCT(dataPoints {_HIST_DP}, aggregationTemporality VARCHAR), "
    f"exponentialHistogram STRUCT(dataPoints {_EXP_DP}, aggregationTemporality VARCHAR), "
    f'"summary" STRUCT(dataPoints {_SUMMARY_DP})'
    ")[]"
    ")[]"
    ")[]"
)

# OTLP AnyValue -> text. Scalars become their plain value; arrays and
# key/value lists keep their JSON form.
_ANYVALUE = (
    # All four scalar paths in one JSON pass (not four), then the first set.
    "coalesce(list_filter(json_extract_string({v}, ['$.stringValue', '$.intValue', '$.doubleValue', '$.boolValue']), "
    "s -> s IS NOT NULL)[1], {v}::VARCHAR)"
)

# Attribute list -> MAP(VARCHAR, VARCHAR). OTLP forbids duplicate keys but
# does not enforce it; keep the first occurrence so one bad record can't
# make a whole partition fail to compact forever.
_ATTR_ENTRIES = "list_transform(coalesce({a}, []), x -> {{'key': x.key, 'value': " + _ANYVALUE.format(v="x.value") + "}})"
_FIRST_OF_EACH_KEY = "(e, i) -> list_position(list_transform(coalesce({a}, []), y -> y.key), e.key) = i"


def _attr_map(a, spill_safe):
    """spill_safe (compaction): always filter to the first occurrence of each
    key. Otherwise (the fast lane, one raw file at a time) take a ~2x faster
    path for records without duplicate keys: same result, but the CASE stops
    DuckDB spilling, so chunks of ~2M records run out of memory (they then
    never compact). One raw file (<= 64 MB gzip) fits either way."""
    entries = _ATTR_ENTRIES.format(a=a)
    first = f"map_from_entries(list_filter({entries}, {_FIRST_OF_EACH_KEY.format(a=a)}))"
    if spill_safe:
        return first
    return (f"CASE WHEN len(list_distinct(list_transform(coalesce({a}, []), y -> y.key))) = len(coalesce({a}, [])) "
            f"THEN map_from_entries({entries}) ELSE {first} END")


_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_service(name):
    return _SAFE.sub("_", name or "unknown")[:128] or "unknown"


# Cap on rows per output file (~150-250 MB of log Parquet), so no single
# file is too big for one query worker to read quickly.
DEFAULT_MAX_ROWS_PER_FILE = 4_000_000


# Log attributes whose values go into each file's bloom filter, alongside
# trace_id. IDs people look up one at a time, not low-cardinality labels.
DEFAULT_BLOOM_ATTRIBUTES = ("request.id",)


SIGNALS = ("logs", "traces", "metrics")


def bloom_fields(signal, bloom_attributes=DEFAULT_BLOOM_ATTRIBUTES):
    """Fields in a signal's bloom filters: trace_id plus the ID attributes for
    log records and spans. Metric points have none, so ID lookups never skip
    a metrics file."""
    return () if signal == "metrics" else ("trace_id", *bloom_attributes)


def compact(signal, input_paths, out_dir, batch_id, arrival_dt, arrival_hour, memory_limit="2GB",
            max_rows_per_file=DEFAULT_MAX_ROWS_PER_FILE, bloom_attributes=DEFAULT_BLOOM_ATTRIBUTES,
            bloom_fpp=bloom.DEFAULT_FPP, id_digests=None, one_bloom=False, spill_safe=True):
    """Compact gzipped OTLP-JSON batches of one signal into Parquet.

    Rows are split by service and by the hour of their own timestamp, so no
    output file spans more than one hour. Rows with no timestamp fall back
    to the arrival hour of the raw partition. A group with more than
    max_rows_per_file rows is split into consecutive slices in sort order,
    part-<batch_id>-000.parquet, -001, ... The split depends only on the row
    count, so the same inputs always produce the same file names.

    Returns one dict per written file: service, dt, hour, part, path, rows,
    min_ts / max_ts (ISO-8601, UTC, microseconds), size_bytes, and a bloom
    filter over the file's bloom_fields(signal) values.

    spill_safe=False: faster parsing for small inputs (see _attr_map).

    one_bloom: every written file gets the same bloom, over all the input's
    rows (the fast lane: one bloom per raw file instead of one per service).

    If id_digests is a dict, it is filled with {(event day, hour): {group: bytes}}:
    the dayfilter digests of every distinct ID in the chunk, for sealing
    day filters.
    """
    fields = bloom_fields(signal, bloom_attributes)
    con = _connect(out_dir, memory_limit)
    try:
        written = []
        for service, dt, hour in load_rows(con, input_paths, arrival_dt, arrival_hour, signal, spill_safe):
            n_rows = _select_group(con, service, dt, hour, signal)
            for part in range(-(-n_rows // max_rows_per_file)):
                rel = (f"dt={dt}/hour={hour}/service={safe_service(service)}/"
                       f"part-{batch_id}-{part:03d}.parquet")
                path = os.path.join(out_dir, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                lo_rn = part * max_rows_per_file
                con.execute(
                    f"""
                    COPY (
                        SELECT * EXCLUDE (rn) FROM grp WHERE rn >= {lo_rn} AND rn < {lo_rn + max_rows_per_file}
                        ORDER BY rn
                    ) TO '{path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)
                    """
                )
                n, lo, hi = con.execute(
                    f"SELECT count(*), strftime(min(ts), '%Y-%m-%dT%H:%M:%S.%fZ'), "
                    f"strftime(max(ts), '%Y-%m-%dT%H:%M:%S.%fZ') FROM read_parquet('{path}')"
                ).fetchone()
                written.append({
                    "service": service, "dt": dt, "hour": hour, "part": part, "relpath": rel,
                    "path": path, "rows": n, "min_ts": lo, "max_ts": hi,
                    "size_bytes": os.path.getsize(path),
                    "bloom": None if one_bloom else _bloom_for(con, "grp", fields, bloom_fpp,
                                                               f"rn >= {lo_rn} AND rn < {lo_rn + max_rows_per_file}"),
                })
        if one_bloom:
            shared = _bloom_for(con, "rows", fields, bloom_fpp)
            for w in written:
                w["bloom"] = shared
        if id_digests is not None and fields:
            _day_digests(con, fields, id_digests)
        return written
    finally:
        con.close()


def compact_logs(input_paths, out_dir, batch_id, arrival_dt, arrival_hour, **kw):
    return compact("logs", input_paths, out_dir, batch_id, arrival_dt, arrival_hour, **kw)


def _connect(work_dir, memory_limit):
    # DuckDB spills big chunks here; it creates the folder but not its parents.
    tmp = os.path.join(work_dir, ".duckdb_tmp")
    os.makedirs(tmp, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute("SET TimeZone='UTC'")
    return con


def load_rows(con, input_paths, arrival_dt, arrival_hour, signal="logs", spill_safe=True):
    """Parse raw OTLP-JSON files of one signal into the temp table `rows`
    (the Parquet schema) and return its (service, dt, hour) groups in order.
    spill_safe: see _attr_map (False only for small inputs, e.g. one raw file)."""
    if signal not in _ROWS_SQL:
        raise ValueError(f"unknown signal {signal!r}")
    input_paths = [plain_json(p) for p in input_paths]
    fallback_ns = f"{_hour_start_us(con, arrival_dt, arrival_hour)} * 1000"
    con.execute(f"CREATE OR REPLACE TEMP TABLE rows AS {_ROWS_SQL[signal](fallback_ns, spill_safe)}", [input_paths])
    return con.execute(
        """
        SELECT service, strftime(ts, '%Y-%m-%d') AS dt, strftime(ts, '%H') AS hour
        FROM rows GROUP BY ALL ORDER BY ALL
        """
    ).fetchall()


_GZIP_MAGIC = b"\x1f\x8b"


def plain_json(path):
    """A raw file as plain newline-delimited JSON, whatever its encoding:
      - one gzip stream (Firehose compressing the records itself; before T6)
      - gzip members back to back (ingest compressing each record; Firehose
        passing them through; T6 onward), which gzip reads as one stream
      - plain JSON (a stream switched to pass-through before ingest compressed)
      - gzip inside gzip (ingest compressing before its stream was switched)
    So files from any point of that changeover read the same. Returns the
    path of a plain copy next to the original (or the original if plain)."""
    with open(path, "rb") as f:
        head = f.read(2)
    if head != _GZIP_MAGIC:
        return path
    out, layer = path, 0
    while True:
        with open(out, "rb") as f:
            if f.read(2) != _GZIP_MAGIC:
                return out
        layer += 1
        nxt = f"{path}.plain{layer}"
        with gzip.open(out, "rb") as src, open(nxt, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if out != path:
            os.remove(out)
        out = nxt


def _read(top, json_type):
    return (f"SELECT unnest({top}) AS r FROM read_json(?, columns={{'{top}': '{json_type}'}}, "
            f"format='newline_delimited', compression='uncompressed', maximum_object_size={MAX_JSON_OBJECT_BYTES})")


def _ns(v):
    return f"nullif(TRY_CAST({v} AS BIGINT), 0)"


def _ts(ns):
    return f"make_timestamp(({ns}) // 1000)"


def _num(v):
    """JSON number, or a numeric string such as "NaN", -> DOUBLE."""
    return f"TRY_CAST(({v})->>'$' AS DOUBLE)"


def _enum(v, names):
    """OTLP JSON enums are integers; accept their names too."""
    cases = " ".join(f"WHEN '{n}' THEN {i}" for i, n in enumerate(names))
    return f"coalesce(TRY_CAST({v} AS INTEGER), CASE {v} {cases} END)"


# obs.* are the platform's own routing labels; the tenant is already in the
# path, so they aren't stored.
def _res_attrs(spill_safe):
    return _attr_map("list_filter(res_attrs, z -> NOT starts_with(z.key, 'obs.'))", spill_safe)
_SERVICE = "coalesce(resource_attributes['service.name'], 'unknown') AS service"
_SPAN_KINDS = ("SPAN_KIND_UNSPECIFIED", "SPAN_KIND_INTERNAL", "SPAN_KIND_SERVER", "SPAN_KIND_CLIENT",
               "SPAN_KIND_PRODUCER", "SPAN_KIND_CONSUMER")
_STATUS_CODES = ("STATUS_CODE_UNSET", "STATUS_CODE_OK", "STATUS_CODE_ERROR")
_TEMPORALITIES = ("AGGREGATION_TEMPORALITY_UNSPECIFIED", "AGGREGATION_TEMPORALITY_DELTA",
                  "AGGREGATION_TEMPORALITY_CUMULATIVE")


def _logs_sql(fallback_ns, spill_safe=True):
    return f"""
        WITH rl AS ({_read('resourceLogs', LOGS_JSON_TYPE)}),
        sl AS (SELECT r.resource.attributes AS res_attrs, unnest(r.scopeLogs) AS sl FROM rl),
        lr AS (SELECT res_attrs, sl.scope.name AS scope_name, unnest(sl.logRecords) AS lr FROM sl),
        flat AS (
            SELECT
                coalesce({_ns('lr.timeUnixNano')}, {_ns('lr.observedTimeUnixNano')}, {fallback_ns}) AS ts_unix_nano,
                {_ns('lr.observedTimeUnixNano')} AS observed_unix_nano,
                {_res_attrs(spill_safe)} AS resource_attributes,
                scope_name,
                lr.severityNumber AS severity_number,
                lr.severityText AS severity_text,
                {_ANYVALUE.format(v='lr.body')} AS body,
                nullif(lr.traceId, '') AS trace_id,
                nullif(lr.spanId, '') AS span_id,
                {_attr_map('lr.attributes', spill_safe)} AS attributes
            FROM lr
        )
        SELECT
            {_ts('ts_unix_nano')} AS ts,
            ts_unix_nano,
            {_ts('observed_unix_nano')} AS observed_ts,
            {_SERVICE},
            severity_number, severity_text, body, trace_id, span_id, scope_name,
            attributes, resource_attributes
        FROM flat
    """


def _traces_sql(fallback_ns, spill_safe=True):
    return f"""
        WITH rs AS ({_read('resourceSpans', SPANS_JSON_TYPE)}),
        ss AS (SELECT r.resource.attributes AS res_attrs, unnest(r.scopeSpans) AS ss FROM rs),
        sp AS (SELECT res_attrs, ss.scope.name AS scope_name, unnest(ss.spans) AS sp FROM ss),
        flat AS (
            SELECT
                coalesce({_ns('sp.startTimeUnixNano')}, {fallback_ns}) AS ts_unix_nano,
                {_ns('sp.endTimeUnixNano')} AS end_unix_nano,
                {_res_attrs(spill_safe)} AS resource_attributes,
                scope_name,
                sp.name AS name,
                {_enum('sp.kind', _SPAN_KINDS)} AS kind,
                coalesce({_enum('sp.status.code', _STATUS_CODES)}, 0) AS status_code,
                nullif(sp.status.message, '') AS status_message,
                nullif(sp.traceId, '') AS trace_id,
                nullif(sp.spanId, '') AS span_id,
                nullif(sp.parentSpanId, '') AS parent_span_id,
                nullif(sp.traceState, '') AS trace_state,
                {_attr_map('sp.attributes', spill_safe)} AS attributes,
                list_transform(coalesce(sp.events, []), ev -> {{
                    'ts': {_ts(_ns('ev.timeUnixNano'))}, 'name': ev.name,
                    'attributes': {_attr_map('ev.attributes', spill_safe)}}}) AS events,
                list_transform(coalesce(sp.links, []), lk -> {{
                    'trace_id': lk.traceId, 'span_id': lk.spanId, 'trace_state': nullif(lk.traceState, ''),
                    'attributes': {_attr_map('lk.attributes', spill_safe)}}}) AS links
            FROM sp
        )
        SELECT
            {_ts('ts_unix_nano')} AS ts,
            ts_unix_nano,
            {_ts('end_unix_nano')} AS end_ts,
            end_unix_nano - ts_unix_nano AS duration_ns,
            {_SERVICE},
            name, kind, status_code, status_message, trace_id, span_id, parent_span_id, trace_state,
            scope_name, attributes, resource_attributes, events, links
        FROM flat
    """


def _metrics_sql(fallback_ns, spill_safe=True):
    base = "res_attrs, scope_name, m.name AS metric_name, m.unit AS unit, m.description AS description"
    counts = "list_transform(coalesce({b}, []), c -> TRY_CAST(c AS BIGINT))"
    stats = (f"TRY_CAST(p.count AS BIGINT) AS count, {_num('p.sum')} AS sum, "
             f"{_num('p.min')} AS min, {_num('p.max')} AS max")
    temporality = "{_t} AS temporality".replace("{_t}", _enum("m.{t}.aggregationTemporality", _TEMPORALITIES))
    return f"""
        WITH rm AS ({_read('resourceMetrics', METRICS_JSON_TYPE)}),
        sm AS (SELECT r.resource.attributes AS res_attrs, unnest(r.scopeMetrics) AS sm FROM rm),
        m AS (SELECT res_attrs, sm.scope.name AS scope_name, unnest(sm.metrics) AS m FROM sm),
        g AS (SELECT {base}, 'gauge' AS metric_type, unnest(m.gauge.dataPoints) AS p FROM m),
        s AS (SELECT {base}, 'sum' AS metric_type, {temporality.format(t='sum')},
                     m.sum.isMonotonic AS is_monotonic, unnest(m.sum.dataPoints) AS p FROM m),
        h AS (SELECT {base}, 'histogram' AS metric_type, {temporality.format(t='histogram')},
                     unnest(m.histogram.dataPoints) AS p FROM m),
        e AS (SELECT {base}, 'exponential_histogram' AS metric_type,
                     {temporality.format(t='exponentialHistogram')},
                     unnest(m.exponentialHistogram.dataPoints) AS p FROM m),
        q AS (SELECT {base}, 'summary' AS metric_type, unnest(m.summary.dataPoints) AS p FROM m),
        points AS (
            SELECT * EXCLUDE (p), p.attributes AS attrs, p.startTimeUnixNano AS start_ns,
                   p.timeUnixNano AS time_ns, p.flags AS flags,
                   coalesce({_num('p.asDouble')}, TRY_CAST(p.asInt AS DOUBLE)) AS value
            FROM (SELECT * FROM g UNION ALL BY NAME SELECT * FROM s)
            UNION ALL BY NAME
            SELECT * EXCLUDE (p), p.attributes AS attrs, p.startTimeUnixNano AS start_ns,
                   p.timeUnixNano AS time_ns, p.flags AS flags, {stats},
                   {counts.format(b='p.bucketCounts')} AS bucket_counts,
                   list_transform(coalesce(p.explicitBounds, []), b -> {_num('b')}) AS explicit_bounds
            FROM h
            UNION ALL BY NAME
            SELECT * EXCLUDE (p), p.attributes AS attrs, p.startTimeUnixNano AS start_ns,
                   p.timeUnixNano AS time_ns, p.flags AS flags, {stats},
                   p.scale AS exp_scale, TRY_CAST(p.zeroCount AS BIGINT) AS exp_zero_count,
                   p.positive.offset AS exp_positive_offset,
                   {counts.format(b='p.positive.bucketCounts')} AS exp_positive_bucket_counts,
                   p.negative.offset AS exp_negative_offset,
                   {counts.format(b='p.negative.bucketCounts')} AS exp_negative_bucket_counts
            FROM e
            UNION ALL BY NAME
            SELECT * EXCLUDE (p), p.attributes AS attrs, p.startTimeUnixNano AS start_ns,
                   p.timeUnixNano AS time_ns, p.flags AS flags,
                   TRY_CAST(p.count AS BIGINT) AS count, {_num('p.sum')} AS sum,
                   list_transform(coalesce(p.quantileValues, []), v -> {{
                       'quantile': {_num('v.quantile')}, 'value': {_num('v.value')}}}) AS quantiles
            FROM q
        ),
        flat AS (
            SELECT *,
                coalesce({_ns('time_ns')}, {fallback_ns}) AS ts_unix_nano,
                {_res_attrs(spill_safe)} AS resource_attributes,
                {_attr_map('attrs', spill_safe)} AS attributes
            FROM points
        )
        SELECT
            {_ts('ts_unix_nano')} AS ts,
            ts_unix_nano,
            {_ts(_ns('start_ns'))} AS start_ts,
            {_SERVICE},
            metric_name, metric_type, unit, description,
            temporality::INTEGER AS temporality, is_monotonic::BOOLEAN AS is_monotonic,
            value::DOUBLE AS value, count::BIGINT AS count, sum::DOUBLE AS sum,
            min::DOUBLE AS min, max::DOUBLE AS max,
            bucket_counts::BIGINT[] AS bucket_counts, explicit_bounds::DOUBLE[] AS explicit_bounds,
            exp_scale::INTEGER AS exp_scale, exp_zero_count::BIGINT AS exp_zero_count,
            exp_positive_offset::INTEGER AS exp_positive_offset,
            exp_positive_bucket_counts::BIGINT[] AS exp_positive_bucket_counts,
            exp_negative_offset::INTEGER AS exp_negative_offset,
            exp_negative_bucket_counts::BIGINT[] AS exp_negative_bucket_counts,
            quantiles::STRUCT(quantile DOUBLE, value DOUBLE)[] AS quantiles,
            TRY_CAST(flags AS INTEGER) AS flags,
            scope_name, attributes, resource_attributes
        FROM flat
    """


_ROWS_SQL = {"logs": _logs_sql, "traces": _traces_sql, "metrics": _metrics_sql}


# Row order within a file: by time; metric points by metric first, so each
# metric's points sit together (better compression, and row-group stats
# can skip other metrics).
_ORDER = {"logs": "ts_unix_nano", "traces": "ts_unix_nano", "metrics": "metric_name, ts_unix_nano"}


def _select_group(con, service, dt, hour, signal="logs"):
    """Temp table `grp`: one (service, event hour) group, numbered in sort order."""
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE grp AS
        SELECT *, row_number() OVER (ORDER BY {_ORDER[signal]}) - 1 AS rn FROM rows
        WHERE service = ? AND strftime(ts, '%Y-%m-%d') = ? AND strftime(ts, '%H') = ?
        """,
        [service, dt, hour],
    )
    return con.execute("SELECT count(*) FROM grp").fetchone()[0]


def _bloom_for(con, table, fields, fpp, where="true"):
    """Bloom filter of the distinct values of `fields` (trace_id, or an
    attribute key) in the rows of `table` matching `where`."""
    parts, params = [], []
    for field in fields:
        if field == "trace_id":
            parts.append(f"SELECT 'trace_id=' || lower(trim(trace_id)) AS t FROM {table} "
                         f"WHERE {where} AND trace_id IS NOT NULL")
        else:
            parts.append(f"SELECT ? || '=' || lower(trim(attributes[?])) FROM {table} "
                         f"WHERE {where} AND attributes[?] IS NOT NULL")
            params += [field, field, field]
    if not parts:
        b = bloom.Bloom.for_capacity(0, fpp)
        b.n = 0
        return b
    sql = "SELECT DISTINCT t FROM (" + " UNION ALL ".join(parts) + ")"
    n = con.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
    b = bloom.Bloom.for_capacity(n, fpp)
    cur = con.execute(sql, params)
    while rows := cur.fetchmany(100_000):
        b.add_many([t for (t,) in rows])
    b.n = n
    return b


def _day_digests(con, fields, out):
    """Digests of the distinct IDs in `rows`, by event hour and group."""
    parts, params = [], []
    for field in fields:
        if field == "trace_id":
            parts.append("SELECT ts, 'trace_id=' || lower(trim(trace_id)) AS t FROM rows WHERE trace_id IS NOT NULL")
        else:
            parts.append("SELECT ts, ? || '=' || lower(trim(attributes[?])) FROM rows WHERE attributes[?] IS NOT NULL")
            params += [field, field, field]
    import numpy as np
    cur = con.execute("SELECT DISTINCT strftime(ts, '%Y-%m-%d'), strftime(ts, '%H'), t FROM ("
                      + " UNION ALL ".join(parts) + ") ORDER BY 1, 2", params)
    while rows := cur.fetchmany(100_000):
        # Batched: hash every term, then split by (day, hour) and group in numpy.
        # Same bytes, in the same order within each group, as one at a time.
        d = np.frombuffer(b"".join(dayfilter.digest(t) for _, _, t in rows), dtype=np.uint8)
        d = d.reshape(-1, dayfilter.DIGEST_BYTES)
        group = d[:, 8] >> 4
        start = 0
        for i in range(1, len(rows) + 1):
            if i == len(rows) or rows[i][:2] != rows[start][:2]:
                seg, seg_group = d[start:i], group[start:i]
                by_group = out.setdefault(rows[start][:2], {})
                for g in np.unique(seg_group):
                    by_group.setdefault(int(g), bytearray()).extend(seg[seg_group == g].tobytes())
                start = i


def _hour_start_us(con, dt, hour):
    return con.execute(
        "SELECT epoch_us(strptime(? || ' ' || ?, '%Y-%m-%d %H'))", [dt, hour]
    ).fetchone()[0]
