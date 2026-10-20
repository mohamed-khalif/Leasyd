"""Correctness gate for the SQL speed-ups (docs/perf-plan.md): every SQL question the app's pages send
(from the capture), on the previous full UTC day, run on each path of obs-query and compared with the
single-worker path. Counts and integers must be equal; floating-point values within 1e-9 relative;
row order compared as returned when the query has ORDER BY, else sorted.

    python3 infra/bench/sql_gate.py PAGE_QUERIES.json TENANT [--paths in_place,fanout] [--day YYYY-MM-DD]

Exit status 1 if any question differs. Prints per question: the paths' times and file counts."""
import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config

lam = boto3.client("lambda", region_name="us-east-1",
                   config=botocore.config.Config(read_timeout=900, retries={"max_attempts": 0}))


def ask(tenant, sql, start, end, path):
    t0 = time.perf_counter()
    r = lam.invoke(FunctionName="obs-query", Payload=json.dumps(
        {"tenant": tenant, "sql": sql, "start": start, "end": end, "sql_path": path, "timing": True}).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    out["_ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def norm(v):
    return round(v, 9) if isinstance(v, float) else v


def differs(a, b, ordered):
    """The first difference between two answers, or None."""
    if a.get("error") or b.get("error"):
        return None if a.get("error") == b.get("error") else f"error {a.get('error')!r} vs {b.get('error')!r}"
    if a["columns"] != b["columns"]:
        return f"columns {a['columns']} vs {b['columns']}"
    ra, rb = a["rows"], b["rows"]
    if len(ra) != len(rb):
        return f"{len(ra)} rows vs {len(rb)}"
    if not ordered:
        key = lambda r: json.dumps([norm(v) for v in r], default=str)   # noqa: E731
        ra, rb = sorted(ra, key=key), sorted(rb, key=key)
    for x, y in zip(ra, rb):
        for u, v in zip(x, y):
            if isinstance(u, float) and isinstance(v, float):
                if not math.isclose(u, v, rel_tol=1e-9, abs_tol=1e-12):
                    return f"row {x} vs {y}"
            elif u != v:
                return f"row {x} vs {y}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture"), ap.add_argument("tenant")
    ap.add_argument("--paths", default="in_place,fanout"), ap.add_argument("--day")
    a = ap.parse_args()
    day = datetime.strptime(a.day, "%Y-%m-%d").replace(tzinfo=timezone.utc) if a.day else \
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    start, end = day.strftime("%Y-%m-%dT%H:%M:%SZ"), (day + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen, failed = [], 0
    for page in json.load(open(a.capture))["pages"]:
        for q in page["queries"]:
            if "sql" in q and q["sql"] not in seen:
                seen.append(q["sql"])
    print(f"{len(seen)} distinct SQL questions, {start} to {end}")
    for i, sql in enumerate(seen):
        base = ask(a.tenant, sql, start, end, "single")
        line = [f"q{i:02d} single {base['_ms'] / 1000:5.1f}s files={(base.get('timing') or {}).get('files')}"]
        for p in a.paths.split(","):
            other = ask(a.tenant, sql, start, end, p)
            d = differs(base, other, ordered="order by" in sql.lower())
            failed += d is not None
            line.append(f"{p} {other['_ms'] / 1000:5.1f}s {'OK' if d is None else 'DIFF ' + d[:200]}"
                        + (f" via={other.get('stats', {}).get('path', 'single')}" if p == "fanout" else ""))
        print("  ".join(line), "|", " ".join(sql.split())[:70], flush=True)
    print("FAILED" if failed else "all equal", f"({failed} differences)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
