# Page benchmark

Each page's queries, captured from the app (`services/web/scripts/perf/capture-queries.cjs`) and
replayed on the real engine for one account (`infra/bench/bench.py`), 6 at a time like the browser.
Times exclude API Gateway and the app API (about 0.1-0.3 s), and the 2 s job polling a query over 20 s gets.

## 2026-10-20 baseline (leasyd-demo, 2 runs, `2026-10-20-baseline.txt`)

Time until a page's last answer (best of 2; first run in brackets where cold starts added over 1 s):

| Page | 1h | 6h | 24h | Slowest query |
|---|---|---|---|---|
| Web | 5.5 | 8.9 | 14.2 | SQL |
| Kubernetes | 6.1 | 8.2 | 12.6 (13.6) | SQL |
| GPUs | 5.2 | 6.8 | 10.4 | SQL |
| Lambda | 5.0 | 5.6 | 9.4 | SQL |
| Services (service map) | 3.4 (4.3) | 5.5 | 9.2 | SQL |
| Built-in services dashboard | 3.6 | 3.9 | 4.6 (6.0) | PromQL |
| Home | 2.4 (6.3) | 2.4 (3.5) | 2.9 (5.3) | traces |
| Agents | 0.9 | 1.0 | 1.3 | traces |
| Logs, Traces, Metrics | 0.7-1.2 | 0.8-1.3 | 0.9-1.2 | |

By query kind, median (p95) in seconds:

| Kind | 1h | 6h | 24h |
|---|---|---|---|
| SQL | 3.7 (6.0) | 4.8 (8.2) | 8.1 (12.6) |
| PromQL | 0.8 (2.7) | 1.0 (2.5) | 1.2 (3.5) |
| JSON (logs, traces, metrics) | 0.1-0.8 | 0.1-0.9 | 0.1-1.1 |

**Finding.** Every slow page is slow because of SQL. `run_sql` runs in one worker Lambda that downloads
every file in the range (64 threads), so its time grows with the range and does not use the
fan-out: the extra Lambda concurrency cannot speed it up as built. JSON queries already fan out
(up to 19 workers here) and stay near 1 s at 24 h.

## 2026-10-20 Phase 0 re-run (`2026-10-20-phase0.txt`, `.json`)

Same code, same capture, about 40 minutes later (12:45-13:15 UTC). SQL pages were 40-110% slower:
Web 1 h 5.5 -> 11.4 s, 24 h 14.2 -> 20.8 s; Kubernetes 24 h 12.6 -> 20.1 s.

**Cause: when the benchmark runs relative to compaction.** An hour of the demo account is about
1,000 fast-lane files per signal (one per service per 30 s flush); compacted, it is 22-34. Compaction
takes an hour 10 minutes after it ends (`GRACE_MINUTES`) and runs every 15 minutes, so for up to about
25 minutes after each hour, the previous hour is still ~1,000 small files as well. Measured at about 13:15:
metrics 05:00-11:00 had 34 files per hour, 12:00 had 1,020 (not yet compacted), 13:00 had 255 so far. By 13:18 the 12:00 hour was compacted to 34.
This morning's run mostly missed that window; Phase 0 ran inside it (1 h questions read up to 1,003
files instead of 595).

Consequences for the plan: benchmark results are only comparable at the same minute past the hour,
so later runs record it and start at :30; Phase 4 (fewer small files) matters more than estimated.

## 2026-10-20 Phase 1: SQL scan fan-out (`2026-10-20-phase1.*`, gate: `2026-10-20-phase1-gate.txt`)

Flags on for leasyd-demo (`SqlScanFanoutAccounts`, `SqlInPlaceAccounts`). Correctness gate on 2026-10-19:
all 27 page SQL questions equal on the single, in-place and fan-out paths (any_value columns excluded:
they vary between runs of the same path). Benchmark started 14:30 UTC; Phase 0 started 12:45, inside
the after-the-hour compaction window, so pages Phase 1 did not change (Logs, Home) were also 20-39%
faster at 1 h from timing alone. The SQL pages' 76-89% drops are well beyond that.

Best of 2, seconds:

| Page | 1 h P0 -> P1 | 6 h P0 -> P1 | 24 h P0 -> P1 | Target 24 h <= 3.5 s |
|---|---|---|---|---|
| Web | 11.4 -> 1.8 | 15.1 -> 2.3 | 20.8 -> 3.0 | met |
| Kubernetes | 10.2 -> 1.4 | 14.5 -> 1.7 | 20.1 -> 4.3 | **not met** |
| GPUs | 8.8 -> 1.2 | 11.8 -> 1.7 | 15.8 -> 2.0 | met |
| Lambda | 6.9 -> 1.2 | 11.6 -> 1.4 | 14.1 -> 1.8 | met |
| Services (map) | 7.3 -> 1.7 | 9.5 -> 1.6 | 11.2 -> 2.2 | met |
| Home | 3.7 -> 2.3 | 3.1 -> 2.5 | 3.9 -> 3.1 | |
| Built-in dashboard | 3.5 -> 3.3 | 5.2 -> 4.3 | 5.2 -> 4.9 | |
| Logs / Traces / Metrics / Agents | 0.7-1.2 | 0.8-1.2 | 0.8-1.4 | |

Kubernetes 24 h: 6 SQL questions, each at the 64-worker cap (1,129 files / 16 per slice), so up to 384
workers at once; cold workers (up to 50 in one question) add 1-1.5 s per question. Remedies: Phase 3, or
fewer, larger slices when a page sends many questions. 24 h Traces (+17%) and Agents (+13%) are JSON
questions Phase 1 didn't change; one 24 h Metrics run hit a 12.9 s straggler worker (the other: 1.0 s).
Noisy pages (best-of-2 apart by > 20%) are re-run 5 times: below.
