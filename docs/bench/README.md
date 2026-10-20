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
