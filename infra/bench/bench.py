"""Page benchmark on the real engine: replays each page's queries (captured from the app by
services/web/scripts/perf/capture-queries.cjs) for a real account, as the browser would send them
(in order, at most 6 at a time), with their time windows moved to now. Per page and range: the
time until its last answer, its slowest query, and what the queries read.

    python3 infra/bench/bench.py PAGE_QUERIES.json TENANT [OUT.json] [--runs 2] [--pages /,/agents] [--workers N]

Calls obs-query directly (as the app API does after authorizing), so the times exclude API
Gateway and the app API (~0.1-0.3 s). --workers N sets each query's most parallel workers
(the engine's default otherwise)."""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config

IN_FLIGHT = 6   # the web app's per-page limit (services/web/src/limiter.ts)
lam = boto3.client("lambda", region_name="us-east-1",
                   config=botocore.config.Config(read_timeout=900, max_pool_connections=32, retries={"max_attempts": 0}))


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def shifted(q, now, captured_end):
    """The query with its time window moved so that the capture's end is now."""
    q = {k: v for k, v in q.items() if k not in ("offsetMs", "async")}
    d = now - captured_end
    for k in ("start", "end"):
        if isinstance(q.get(k), str):
            q[k] = iso(parse(q[k]) + d)
    if isinstance(q.get("time"), (int, float)):
        q["time"] = q["time"] + d.total_seconds()
    return q


def run_one(q, tenant, workers):
    body = {**q, "tenant": tenant, **({"workers": workers} if workers and "sql" not in q and "promql" not in q else {})}
    t0 = time.perf_counter()
    r = lam.invoke(FunctionName="obs-query", Payload=json.dumps(body).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    ms = (time.perf_counter() - t0) * 1000
    st = out.get("stats") or {}
    kind = "sql" if "sql" in q else "promql" if "promql" in q else q.get("signal", "?")
    return {"kind": kind, "ms": round(ms), "error": (out.get("error") or out.get("errorMessage") or "")[:160],
            "files": st.get("files"), "bytes": st.get("bytes"), "workers": st.get("workers"),
            "rows": len(out.get("rows", [])) if isinstance(out.get("rows"), list) else None,
            "what": (q.get("sql") or q.get("promql") or json.dumps({k: q[k] for k in q if k in ("signal", "group_by", "where", "search")}))[:140]}


def run_page(page, tenant, workers):
    qs = page["queries"]
    if not qs:
        return {"ms": 0, "queries": []}
    ends = [parse(q["end"]) for q in qs if isinstance(q.get("end"), str)]
    captured_end, now = (max(ends) if ends else datetime.now(timezone.utc)), datetime.now(timezone.utc)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(IN_FLIGHT) as pool:
        results = list(pool.map(lambda q: run_one(shifted(q, now, captured_end), tenant, workers), qs))
    return {"ms": round((time.perf_counter() - t0) * 1000), "queries": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture"), ap.add_argument("tenant"), ap.add_argument("out", nargs="?")
    ap.add_argument("--runs", type=int, default=2), ap.add_argument("--pages"), ap.add_argument("--ranges")
    ap.add_argument("--workers", type=int)
    a = ap.parse_args()
    pages = json.load(open(a.capture))["pages"]
    if a.pages:
        pages = [p for p in pages if p["route"] in a.pages.split(",")]
    if a.ranges:
        pages = [p for p in pages if p["range"] in a.ranges.split(",")]
    report = []
    for p in pages:
        runs = [run_page(p, a.tenant, a.workers) for _ in range(a.runs)]
        best, first = min(r["ms"] for r in runs), runs[0]
        slow = max(first["queries"], key=lambda q: q["ms"], default=None)
        errs = sorted({q["error"] for r in runs for q in r["queries"] if q["error"]})
        report.append({"route": p["route"], "range": p["range"], "n": len(p["queries"]), "first_ms": first["ms"],
                       "best_ms": best, "slowest": slow, "errors": errs, "runs": runs})
        print(f"{p['range']:>4} {p['route']:<30} {len(p['queries']):>3}q  first {first['ms']/1000:6.1f}s  best {best/1000:6.1f}s"
              + (f"  slowest {slow['ms']/1000:5.1f}s {slow['kind']} files={slow['files']} {slow['what'][:60]!r}" if slow else "")
              + (f"  ERR {errs[0][:80]}" if errs else ""), flush=True)
    if a.out:
        json.dump({"tenant": a.tenant, "at": iso(datetime.now(timezone.utc)), "workers": a.workers, "pages": report},
                  open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
