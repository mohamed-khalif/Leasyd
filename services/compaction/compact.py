"""Turn raw OTLP JSON batch files into sorted Parquet, one file per
(service, event hour). Pure local-file logic, no AWS calls, so it can be
tested without S3."""

import os
import re

import duckdb

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


def compact_logs(input_paths, out_dir, batch_id, arrival_dt, arrival_hour, memory_limit="2GB"):
    """Compact gzipped OTLP-JSON log batches into Parquet.

    Rows are split by service and by the hour of their own timestamp, so no
    output file spans more than one hour. Records with no timestamp fall back
    to the arrival hour of the raw partition.

    Returns one dict per written file: service, dt, hour, path, rows,
    min_ts / max_ts (ISO-8601, UTC, microseconds) and size_bytes.
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
                    {_ATTR_MAP.format(a='res_attrs')} AS resource_attributes,
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
            rel = f"dt={dt}/hour={hour}/service={safe_service(service)}/part-{batch_id}.parquet"
            path = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            con.execute(
                f"""
                COPY (
                    SELECT * FROM rows
                    WHERE service = ? AND strftime(ts, '%Y-%m-%d') = ? AND strftime(ts, '%H') = ?
                    ORDER BY ts_unix_nano
                ) TO '{path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)
                """,
                [service, dt, hour],
            )
            n, lo, hi = con.execute(
                f"SELECT count(*), strftime(min(ts), '%Y-%m-%dT%H:%M:%S.%fZ'), "
                f"strftime(max(ts), '%Y-%m-%dT%H:%M:%S.%fZ') FROM read_parquet('{path}')"
            ).fetchone()
            written.append({
                "service": service, "dt": dt, "hour": hour, "relpath": rel, "path": path,
                "rows": n, "min_ts": lo, "max_ts": hi, "size_bytes": os.path.getsize(path),
            })
        return written
    finally:
        con.close()


def _hour_start_us(con, dt, hour):
    return con.execute(
        "SELECT epoch_us(strptime(? || ' ' || ?, '%Y-%m-%d %H'))", [dt, hour]
    ).fetchone()[0]
