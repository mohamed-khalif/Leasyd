"""Turn raw OTLP JSON batch files into sorted Parquet, one file per
(service, event hour). Pure local-file logic, no AWS calls, so it can be
tested without S3."""

import os
import re

import duckdb

import bloom

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

# OTLP AnyValue -> text. Scalars become their plain value; arrays and
# key/value lists keep their JSON form.
_ANYVALUE = (
    "coalesce({v}->>'$.stringValue', {v}->>'$.intValue', {v}->>'$.doubleValue', "
    "{v}->>'$.boolValue', {v}::VARCHAR)"
)

# Attribute list -> MAP(VARCHAR, VARCHAR). OTLP forbids duplicate keys but
# does not enforce it; keep the first occurrence so one bad record can't
# make a whole partition fail to compact forever.
_ATTR_MAP = (
    "map_from_entries(list_filter("
    "list_transform(coalesce({a}, []), x -> {{'key': x.key, 'value': " + _ANYVALUE.format(v="x.value") + "}}), "
    "(e, i) -> list_position(list_transform(coalesce({a}, []), y -> y.key), e.key) = i))"
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_service(name):
    return _SAFE.sub("_", name or "unknown")[:128] or "unknown"


# Cap on rows per output file (~150-250 MB of log Parquet), so no single
# file is too big for one query worker to read quickly.
DEFAULT_MAX_ROWS_PER_FILE = 4_000_000


# Log attributes whose values go into each file's bloom filter, alongside
# trace_id. IDs people look up one at a time, not low-cardinality labels.
DEFAULT_BLOOM_ATTRIBUTES = ("request.id",)


def compact_logs(input_paths, out_dir, batch_id, arrival_dt, arrival_hour, memory_limit="2GB",
                 max_rows_per_file=DEFAULT_MAX_ROWS_PER_FILE, bloom_attributes=DEFAULT_BLOOM_ATTRIBUTES,
                 bloom_fpp=bloom.DEFAULT_FPP):
    """Compact gzipped OTLP-JSON log batches into Parquet.

    Rows are split by service and by the hour of their own timestamp, so no
    output file spans more than one hour. Records with no timestamp fall back
    to the arrival hour of the raw partition. A group with more than
    max_rows_per_file rows is split into consecutive time slices,
    part-<batch_id>-000.parquet, -001, ... The split depends only on the row
    count, so the same inputs always produce the same file names.

    Returns one dict per written file: service, dt, hour, part, path, rows,
    min_ts / max_ts (ISO-8601, UTC, microseconds), size_bytes, and a bloom
    filter over the file's trace_id and bloom_attributes values.
    """
    con = duckdb.connect()
    try:
        tmp = os.path.join(out_dir, ".duckdb_tmp")
        con.execute(f"SET memory_limit='{memory_limit}'")
        con.execute(f"SET temp_directory='{tmp}'")
        con.execute("SET TimeZone='UTC'")

        fallback_us = _hour_start_us(con, arrival_dt, arrival_hour)
        con.execute(
            f"""
            CREATE TEMP TABLE rows AS
            WITH rl AS (
                SELECT unnest(resourceLogs) AS rl
                FROM read_json(?, columns={{'resourceLogs': '{LOGS_JSON_TYPE}'}},
                               format='newline_delimited', compression='gzip',
                               maximum_object_size={MAX_JSON_OBJECT_BYTES})
            ),
            sl AS (
                SELECT rl.resource.attributes AS res_attrs, unnest(rl.scopeLogs) AS sl FROM rl
            ),
            lr AS (
                SELECT res_attrs, sl.scope.name AS scope_name, unnest(sl.logRecords) AS lr FROM sl
            ),
            flat AS (
                SELECT
                    coalesce(nullif(TRY_CAST(lr.timeUnixNano AS BIGINT), 0),
                             nullif(TRY_CAST(lr.observedTimeUnixNano AS BIGINT), 0),
                             {fallback_us} * 1000) AS ts_unix_nano,
                    nullif(TRY_CAST(lr.observedTimeUnixNano AS BIGINT), 0) AS observed_unix_nano,
                    -- obs.* are the collector's own routing labels (tenant, S3 prefix);
                    -- the tenant is already in the path, so they aren't stored.
                    {_ATTR_MAP.format(a="list_filter(res_attrs, z -> NOT starts_with(z.key, 'obs.'))")} AS resource_attributes,
                    scope_name,
                    lr.severityNumber AS severity_number,
                    lr.severityText AS severity_text,
                    {_ANYVALUE.format(v='lr.body')} AS body,
                    nullif(lr.traceId, '') AS trace_id,
                    nullif(lr.spanId, '') AS span_id,
                    {_ATTR_MAP.format(a='lr.attributes')} AS attributes
                FROM lr
            )
            SELECT
                make_timestamp(ts_unix_nano // 1000) AS ts,
                ts_unix_nano,
                make_timestamp(observed_unix_nano // 1000) AS observed_ts,
                coalesce(resource_attributes['service.name'], 'unknown') AS service,
                severity_number, severity_text, body, trace_id, span_id, scope_name,
                attributes, resource_attributes
            FROM flat
            """,
            [input_paths],
        )

        groups = con.execute(
            """
            SELECT service, strftime(ts, '%Y-%m-%d') AS dt, strftime(ts, '%H') AS hour
            FROM rows GROUP BY ALL ORDER BY ALL
            """
        ).fetchall()

        written = []
        for service, dt, hour in groups:
            con.execute(
                """
                CREATE OR REPLACE TEMP TABLE grp AS
                SELECT *, row_number() OVER (ORDER BY ts_unix_nano) - 1 AS rn FROM rows
                WHERE service = ? AND strftime(ts, '%Y-%m-%d') = ? AND strftime(ts, '%H') = ?
                """,
                [service, dt, hour],
            )
            n_rows = con.execute("SELECT count(*) FROM grp").fetchone()[0]
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
                    "bloom": _bloom_for(con, lo_rn, lo_rn + max_rows_per_file, bloom_attributes, bloom_fpp),
                })
        return written
    finally:
        con.close()


def _bloom_for(con, lo_rn, hi_rn, attributes, fpp):
    """Bloom filter of the distinct indexed values in rows [lo_rn, hi_rn) of grp."""
    parts = [f"SELECT 'trace_id=' || lower(trim(trace_id)) AS t FROM grp "
             f"WHERE rn >= {lo_rn} AND rn < {hi_rn} AND trace_id IS NOT NULL"]
    params = []
    for key in attributes:
        parts.append(f"SELECT ? || '=' || lower(trim(attributes[?])) FROM grp "
                     f"WHERE rn >= {lo_rn} AND rn < {hi_rn} AND attributes[?] IS NOT NULL")
        params += [key, key, key]
    sql = "SELECT DISTINCT t FROM (" + " UNION ALL ".join(parts) + ")"
    n = con.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
    b = bloom.Bloom.for_capacity(n, fpp)
    cur = con.execute(sql, params)
    while rows := cur.fetchmany(100_000):
        for (t,) in rows:
            b.add(t)
    b.n = n
    return b


def _hour_start_us(con, dt, hour):
    return con.execute(
        "SELECT epoch_us(strptime(? || ' ' || ?, '%Y-%m-%d %H'))", [dt, hour]
    ).fetchone()[0]
