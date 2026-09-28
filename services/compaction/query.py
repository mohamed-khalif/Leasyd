"""Query engine (Phases 4-5): answer a query over one tenant's data.

    {"tenant": "acme",                         # required (from the caller's credentials in an API)
     "signal": "logs",                         # logs | traces | metrics
     "start": "2026-09-27T00:00:00Z", "end": "2026-09-27T23:59:59Z",
     "services": ["checkout"],                 # optional; default every service
     "where": [{"field": "severity_number", "op": ">=", "value": 17},
               {"field": "attributes.http.route", "op": "=", "value": "/api/cart"},
               {"field": "body", "op": "contains", "value": "timeout"}],
     "match": {"trace_id": "4bf9..."},         # optional ID lookup (bloom-pruned, then filtered exactly)
     "group_by": ["attributes.http.route"],    # aggregate mode ...
     "aggs": [{"fn": "count"}, {"fn": "p95", "field": "attributes.duration_ms"}],
     "search": {"limit": 100},                 # ... or search mode: newest matching rows
     "order": "desc", "limit": 100,            # aggregate results: by the first agg
     "workers": 16}                            # most parallel workers to use

Fields: a column of the signal's table (e.g. severity_number, body, name,
duration_ns, metric_name, value), or attributes.<key> / resource.<key>.
Ops: = != < <= > >= in contains exists. Aggregates: count, sum, min, max,
avg, p50, p90, p95, p99 (percentiles from a log-bucket histogram, ~2.5%
relative error, so they merge exactly across workers).

The query is never SQL from the caller: fields are checked against the
signal's columns and every value is a bound parameter.

coordinator: runs the index lookup (the files that can hold matching rows,
raw and Parquet), splits them into balanced chunks, runs one worker per
chunk in parallel, and merges their partial results.
worker: downloads its files with the tenant-scoped credentials (IAM refuses
anything outside the tenant), loads them into DuckDB, runs the compiled
query and returns partial aggregates or rows.
"""

import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from datetime import time as dt_time
from decimal import Decimal

import boto3
import botocore.config
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

import compact
import layout
import lookup

WORKER_FUNCTION = os.environ.get("QUERY_WORKER_FUNCTION", "obs-query-worker")
MAX_WORKERS = int(os.environ.get("QUERY_MAX_WORKERS", "64"))
TARGET_BYTES_PER_WORKER = int(os.environ.get("QUERY_BYTES_PER_WORKER", str(256 * 1024 * 1024)))
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
AGGS = {"count", "sum", "min", "max", "avg", "p50", "p90", "p95", "p99"}
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


def _num(expr):
    return f"TRY_CAST({expr} AS DOUBLE)"


def compile_query(q):
    """-> (sql, params, kind) for a worker. kind: "search" or "aggregate"."""
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
        elif op == "contains":
            where.append(f"{expr}::VARCHAR ILIKE ?")
            params.append("%" + str(cond["value"]).replace("%", r"\%").replace("_", r"\_") + "%")
        elif op == "in":
            vals = list(cond["value"])
            if not vals:
                raise BadQuery("'in' needs values")
            where.append(f"{expr}::VARCHAR IN ({', '.join('?' * len(vals))})")
            params += [str(v) for v in vals]
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
    group_params, groups = [], []
    for g in q.get("group_by") or []:
        groups.append(_field(signal, g, group_params))
    select, agg_params = [f"{g} AS g{i}" for i, g in enumerate(groups)], []
    for i, a in enumerate(aggs):
        fn = a.get("fn")
        if fn not in AGGS:
            raise BadQuery(f"unknown aggregate {fn!r}; one of {sorted(AGGS)}")
        if fn == "count":
            select.append(f"count(*) AS a{i}")
            continue
        field_params = []
        x = _num(_field(signal, a.get("field"), field_params))
        if fn == "avg":
            parts = [f"sum({x}) AS a{i}_sum", f"count({x}) AS a{i}_n"]
        elif fn.startswith("p"):
            # log-bucket histogram {bucket: count}; bucket -inf holds values <= 0
            parts = [f"histogram(CASE WHEN {x} > 0 THEN floor(ln({x}) / ln({HIST_BASE}))::INTEGER "
                     f"WHEN {x} IS NOT NULL THEN -2147483648 END) AS a{i}"]
        else:
            parts = [f"{fn}({x}) AS a{i}"]
        select += parts
        # The field appears several times; bind its parameters each time.
        agg_params += field_params * sum(p.count(x) for p in parts)
    group_sql = f" GROUP BY {', '.join(str(i + 1) for i in range(len(groups)))}" if groups else ""
    sql = f"SELECT {', '.join(select)} FROM t WHERE {where_sql}{group_sql}"
    # Parameters in text order: SELECT (groups, then aggs), then WHERE.
    return sql, group_params + agg_params + params, "aggregate"


def _naive(ts):
    """ISO-8601 -> naive UTC timestamp text, as stored in Parquet."""
    return lookup._parse(ts).strftime("%Y-%m-%d %H:%M:%S.%f")


# -------------------------------------------------------------- worker

def worker(event, context):
    return run_worker(event)


def run_worker(event):
    """Read this chunk's files as the tenant, load them, run the query.

    Parquet is read in place ("ranges", the default): DuckDB asks for the
    footer and just the column chunks the query needs, fetched as S3 range
    requests. "download" fetches whole files first (the Phase 4 baseline).
    Raw files are always downloaded: gzipped JSON has to be read in full."""
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
        in_place = [f for f in files if f["kind"] == "parquet" and mode == "ranges"]

        def fetch(i_f):
            i, f = i_f
            ext = ".parquet" if f["kind"] == "parquet" else ".json.gz"
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
            pq = [p for f, p in local if f["kind"] == "parquet"]
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
            raw = [(f, p) for f, p in local if f["kind"] == "raw"]
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
            rows = cur.fetchmany(MAX_ROWS + 1)
            scanned = con.execute("SELECT count(*) FROM t").fetchone()[0] if event.get("count_scanned") else None
        finally:
            con.close()
        return {"kind": kind, "columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:MAX_ROWS]],
                "truncated": len(rows) > MAX_ROWS,
                "stats": {"files": len(files), "read_mode": mode,
                          "bytes": sum(os.path.getsize(p) for _, p in local) + ranges.bytes_read,
                          "range_requests": ranges.requests,
                          "download_ms": round((t_dl - t0) * 1000), "query_ms": round((time.perf_counter() - t_dl) * 1000),
                          "rows_scanned": scanned}}
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    try:
        return run(event)
    except BadQuery as e:
        return {"error": str(e)}


def run(q, invoke_worker=None):
    t0 = time.perf_counter()
    compile_query(q)  # reject bad queries before any work
    found = lookup.lookup(tenant=q["tenant"], start=q["start"], end=q["end"], signal=q.get("signal", "logs"),
                          services=q.get("services"), match=q.get("match"))
    t_lookup = time.perf_counter()
    chunks = plan_chunks(found["files"], int(q.get("workers", MAX_WORKERS)))
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


def plan_chunks(files, max_workers):
    """Split files into <= max_workers chunks of similar total size (largest first)."""
    files = sorted({f["file_path"]: f for f in files}.values(), key=lambda f: -f["size_bytes"])
    if not files:
        return []
    total = sum(f["size_bytes"] for f in files)
    n = max(1, min(max_workers, MAX_WORKERS, len(files), math.ceil(total / TARGET_BYTES_PER_WORKER)))
    bins = [[0, []] for _ in range(n)]
    for f in files:
        b = min(bins, key=lambda x: x[0])
        b[0] += f["size_bytes"]
        b[1].append(f)
    return [b[1] for b in bins if b[1]]


def _invoke_worker(payload):
    r = lam.invoke(FunctionName=WORKER_FUNCTION, Payload=json.dumps(payload).encode())
    out = json.loads(r["Payload"].read())
    if r.get("FunctionError"):
        raise RuntimeError(f"query worker failed: {out}")
    return out


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
                elif fn.startswith("p"):
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
                    elif fn in ("count", "sum"):
                        acc[i] += v
                    elif fn == "min":
                        acc[i] = min(acc[i], v)
                    elif fn == "max":
                        acc[i] = max(acc[i], v)
    rows = []
    for key, acc in merged.items():
        out = list(key)
        for a, v in zip(aggs, acc):
            if a["fn"] == "avg":
                out.append(v[0] / v[1] if v and v[1] else None)
            elif a["fn"].startswith("p"):
                out.append(_percentile(v or {}, int(a["fn"][1:]) / 100))
            else:
                out.append(v if v is not None else (0 if a["fn"] == "count" else None))
        rows.append(out)
    i = len(groups)   # order by the first aggregate; empty values last either way
    if q.get("order", "desc") == "desc":
        rows.sort(key=lambda r: (r[i] is not None, r[i] or 0), reverse=True)
    else:
        rows.sort(key=lambda r: (r[i] is None, r[i] or 0))
    cols = list(groups) + [a["fn"] if a["fn"] == "count" else f"{a['fn']}({a.get('field')})" for a in aggs]
    return {"columns": cols, "rows": rows[:min(int(q.get("limit", 100)), MAX_ROWS)]}


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
