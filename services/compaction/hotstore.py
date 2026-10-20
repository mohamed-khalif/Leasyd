"""The hot store (docs/hot-store.md): every flagged account's last KEEP_S of data, in memory, as rows of
the same tables the files hold, answering the coordinator's questions about recent time exactly as a query
worker answers about files (query.answer_view, query.scan_view).

Rows come from the hot Kinesis stream (services/hot/server.py): each record is a batch exactly as ingest
sent it to Firehose. A (tenant, signal) is "covered" from the arrival time of the first record seen for it
since the store started reading, or that start if later records show it was already sending: the
coordinator only asks about events at or after that time (plus a margin), and S3 answers for the rest.
"""
import base64
import gzip
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone

import duckdb

import compact
import layout
import query

KEEP_S = int(os.environ.get("HOT_KEEP_S", str(2 * 3600)))
SIGNALS = ("logs", "traces", "metrics")


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class HotStore:
    def __init__(self, keep_s=KEEP_S, reading_since=None):
        self.db = duckdb.connect()   # in memory; each user takes a cursor (its own temp tables)
        self.db.execute("SET TimeZone='UTC'")
        self.keep_s = keep_s
        self.reading_since = time.time() - keep_s if reading_since is None else reading_since
        self.covered = {}    # (tenant, signal) -> epoch from which this store has every record
        self.lock = threading.Lock()   # table creation and eviction
        self.tables = set()

    # ------------------------------------------------------------- writes

    def add(self, tenant, signal, records, arrived):
        """records: ingest's record bytes (plain or gzip JSON) for one tenant and signal, the earliest of
        which reached the stream at epoch `arrived`."""
        layout.check_tenant(tenant)
        if signal not in SIGNALS or not records:
            return 0
        work = tempfile.mkdtemp()
        try:
            path = os.path.join(work, "batch.json")
            with open(path, "wb") as f:
                for r in records:
                    r = gzip.decompress(r) if r[:2] == b"\x1f\x8b" else r
                    f.write(r if r.endswith(b"\n") else r + b"\n")
            cur = self.db.cursor()
            try:
                at = datetime.fromtimestamp(arrived, timezone.utc)
                compact.load_rows(cur, [path], at.strftime("%Y-%m-%d"), at.strftime("%H"), signal, spill_safe=False)
                table = f"hot_{signal}"
                with self.lock:
                    if table not in self.tables:
                        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT ''::VARCHAR AS tenant, * FROM rows LIMIT 0")
                        self.tables.add(table)
                n = cur.execute("SELECT count(*) FROM rows").fetchone()[0]
                cur.execute(f"INSERT INTO {table} BY NAME SELECT ? AS tenant, * FROM rows", [tenant])
            finally:
                cur.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)
        with self.lock:
            self.covered.setdefault((tenant, signal), max(self.reading_since, arrived))
        return n

    def evict(self, now=None):
        """Drop rows older than keep_s (by event time)."""
        cutoff = _iso((now or time.time()) - self.keep_s)
        with self.lock:
            for table in self.tables:
                self.db.execute(f"DELETE FROM {table} WHERE ts < ?::TIMESTAMP", [cutoff[:-1]])

    # -------------------------------------------------------------- reads

    def status(self):
        with self.lock:
            covered = {}
            for (tenant, signal), since in self.covered.items():
                covered.setdefault(tenant, {})[signal] = _iso(since)
            rows = {t: self.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in sorted(self.tables)}
        return {"covered": covered, "rows": rows, "keep_s": self.keep_s}

    def _view(self, cur, tenant, table):
        """The temp view t: this tenant's rows of a table (a signal's, or a summary made from them)."""
        layout.check_tenant(tenant)
        signal = query.SQL_TABLES.get(table, table)
        source, derive = compact.SUMMARIES.get(table, (signal, None))
        if f"hot_{source}" in self.tables:
            base = f"(SELECT * EXCLUDE (tenant) FROM hot_{source} WHERE tenant = '{tenant}')"
        else:   # nothing yet: the table's columns, no rows
            empty = tempfile.mkdtemp()
            try:
                path = os.path.join(empty, "empty.json")
                with open(path, "w") as f:
                    f.write({"logs": '{"resourceLogs": []}', "traces": '{"resourceSpans": []}',
                             "metrics": '{"resourceMetrics": []}'}[source] + "\n")
                compact.load_rows(cur, [path], "2000-01-01", "00", source)
                cur.execute("CREATE OR REPLACE TEMP TABLE empty AS SELECT * FROM rows")
            finally:
                shutil.rmtree(empty, ignore_errors=True)
            base = "empty"
        cur.execute("CREATE OR REPLACE TEMP VIEW t AS " + (derive.format(src=base) if derive else f"SELECT * FROM {base}"))

    def worker(self, q):
        """A JSON question about [q.start, q.end) -> query.run_worker's answer shape."""
        t0 = time.perf_counter()
        cur = self.db.cursor()
        try:
            self._view(cur, q["tenant"], q.get("signal", "logs"))
            sql, params, kind = query.compile_query(q)
            out = query.answer_view(cur, q, sql, params, kind)
        finally:
            cur.close()
        out["stats"] = {"files": 0, "hot": True, "query_ms": round((time.perf_counter() - t0) * 1000)}
        return out

    def scan(self, job):
        """A SQL scan slice (query.run_scan_worker's job, no files) -> the kept rows as Parquet (base64)."""
        t0 = time.perf_counter()
        work, cur = tempfile.mkdtemp(), self.db.cursor()
        try:
            self._view(cur, job["tenant"], job["scan"]["table"])
            out = os.path.join(work, "scan.parquet")
            query.scan_view(cur, job["scan"], job["start"], job["end"], out)
            size = os.path.getsize(out)
            stats = {"files": 0, "hot": True, "out_bytes": size, "query_ms": round((time.perf_counter() - t0) * 1000)}
            if size > query.SCAN_MAX_BYTES:
                return {"too_big": True, "stats": stats}
            with open(out, "rb") as f:
                return {"parquet": base64.b64encode(f.read()).decode(), "stats": stats}
        finally:
            cur.close()
            shutil.rmtree(work, ignore_errors=True)
