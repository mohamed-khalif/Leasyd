# Phase 7: verification and results of the web app clean-up

Date: 2026-10-18. Covers Phases 4–6 and the Phase 6 extras (docs/refactor-progress.md), plus what Phase 7 found and fixed.

## Summary in plain words

- **Every page now asks the server for one consistent time window.**
  - Before, Home asked 20 questions over up to 7 slightly different "nows", so its numbers could disagree.
  - Now there is one "now" per page everywhere. Home, Dashboards and SLOs were the last pages fixed.
- **A page never floods the server.** At most 6 data queries run at once per page. Home used to send 22 at once.
- **Refresh is as fast or faster.** Kubernetes went from 0.77 s to 0.42 s and GPUs from 0.61 s to 0.33 s, measured on the same machine.
- **First page loads are about as fast as before**, within ±10% on most pages and faster on some.
  - Phase 7 found and fixed a 300 ms delay on the first visit to every page.
  - Per-page code loading had introduced that delay.
- **The number of requests didn't change.** That is expected: the decision was "no cache" (docs/caching-decision.md), and fewer requests would need a cache.
- **Production (AWS) since the final deploy, with low traffic:**
  - no errors;
  - the query API's slowest 5% went from 2.8 s to 2.3 s;
  - the query worker's slowest 5% went from 1.0 s to 0.7 s;
  - the median API time went up, from 0.40 s to 0.58 s. With this little traffic the cause can't be told apart from the mix of traffic (see "Production").

## How it was measured

- **Same method as the original audit** (docs/code-audit.md §1):
  - each page is opened fresh with the 15-minute range, then Refresh is pressed once;
  - the backend is the mock, plus the real query engine for Kubernetes and GPU data (`services/web/mock/engine_server.py`, 6 hours of the demo cluster).
- **What is compared:**
  - *Before* is commit d2a42dc, the last commit before Phase 6.
  - *After* is this branch.
  - Both run side by side on the same machine with the same data.
- **Production builds:**
  - each version is built with `vite build --mode mock` and served with its own mock backend (`services/web/scripts/perf/serve.cjs`);
  - requests are read from the browser's network log (`services/web/scripts/perf/measure.cjs`).
- **Why not the dev server:** it loads the per-page code as many small files, which made the new version look about 300 ms slower than it really is.
- **Runs:** 3 per version, alternating between versions; the median is shown. ¹ marks pages re-measured with 5 runs after the last fix.
- **Check of the method:** the "before" counts match the original audit (Home 23 requests and 20 queries; Dashboards 18 and 15).
- **Limits:** the mock answers each request in 150–400 ms, so the times show proportions, not production numbers. Differences under about 50 ms are noise.

## Per page

Column meanings:
- **Window ends:** how many different "nows" the page's queries used. 1 is right.
- **Most in flight:** the most requests open at once. Data queries are capped at 6; the rest are account and sign-in requests.

| Page | Requests on load | Query requests | Window ends | Most in flight | First load, ms | Refresh, ms |
|---|---|---|---|---|---|---|
| Home | 23 | 20 | 4 → **1** | 22 → **8** | 1284 → 1363 | 1138 → 1140 |
| Logs | 5 | 3 | 1 | 4 | 495 → 571 | 396 → 316 |
| Traces | 5 | 3 | 1 | 4 | 531 → 572 | 454 → 445 |
| Metrics | 5 | 3 | 1 | 4 | 495 → 500 | 422 → 353 |
| Service map | 4 | 2 | 1 | 3 | 397 → 355 | 282 → 296 |
| Web (real users) | 10 | 8 | 1 | 7 → **6** | 899 → 874 | 548 → 556 |
| AWS Lambda | 4 | 2 | 1 | 3 | 371 → 374 | 295 → 288 |
| Kubernetes | 8 | 6 | 1 | 6 | 980 → 1008 | 774 → **415** |
| GPUs | 7 | 4 | 1 | 5 | 712 → 705 | 605 → **333** |
| Dashboard (built-in Service overview) | 18 | 15 | 2 → **1** | 11 → **8** | 932 → 902 | 805 → 806 |
| Query Builder | 4 | 2 | 1 | 3 | 491 → 377 | 309 → 288 |
| SQL | 2 | 0 | 0 | 1 | 332 → 324 | – |
| Alerts | 6 | 0 | 0 | 5 | 331 → 335 | 239 → 245 |
| Synthetic checks | 6 | 3 | 1 | 5 | 514 → 458 | 407 → 316 |
| SLOs | 10 | 6 | 2 → **1** | 6 | 678 → 711 | 434 → 429 |
| Leasyd AI | 11 | 7 | 0 | 10 → **9** | 656 → 624 | 553 → 546 |
| Get started | 6 | 3 | 1 | 5 | 556 → 534 | – |
| Drop rules | 9 | 4 | 1 → 2 | 5 | 695 → 670 | 248 → 240 |
| Settings | 4 → **3** | 0 | 0 | 2 | 741 → 532 ¹ | 239 → 231 |
| Usage & cost (was Usage) | 6 → 7 | 3 | 1 | 5 → 4 | 501 → 532 ¹ | 378 → 397 |

Notes:
- **Request counts are unchanged** except:
  - Settings has one fewer: GPU prices now load only on their own page.
  - Usage & cost has one more: it moved into Settings, which loads the account, and the trial banner loads it too.
- **Drop rules shows 2 window ends.** Each rule's "what would this drop" preview reads the clock itself (`services/web/src/pages/DropRules.tsx:67`), as it did before. The old run happened to land on the same millisecond. It's harmless (a 24-hour preview per rule), so it's left as is.
- **Kubernetes and GPU discovery** deliberately looks only at the last 15 minutes of the range, to see what is sending data now (`services/web/src/infra/useInfra.ts:12-18`). It isn't counted as a second window.
- **First load includes downloading the page's code.**
  - Since step 6.7 there is one code file per page; the first download went from 224 kB to 109 kB (gzip).
  - Home is the slowest page in both versions because of its 20 queries.
  - With 6 queries at a time, Home is 6% slower in the mock (1.28 → 1.36 s), where every query takes the same time.

## Navigation

| Path | Requests | Window ends | Most in flight | Until settled, ms |
|---|---|---|---|---|
| Home → Traces → Home | 47 → 47 | 6 → 3 (one per visit) | 22 → 8 | 5680 → 5838 |
| Kubernetes → GPUs → Kubernetes → GPUs | 24 → 24 | 4 → 4 (one per visit) | 6 → 5 | 7204 → 7376 |

- Each visit asks the server again: no cache, by decision.
- The times include the 1.5 s the script waits for quiet after each step.

## Production (AWS, CloudWatch)

| Lambda | Window | Runs | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| obs-query-api | 7 days before (10-10 → 10-17) | 1,426 | 401 ms | 2,806 ms | 4,790 ms | – |
| obs-query-api | since the final deploy (10-17 23:15 → 10-18 18:00 UTC) | 394 | 585 ms | 2,348 ms | 3,650 ms | 0 |
| obs-query-worker | 7 days before | 1,114 | 485 ms | 993 ms | 3,202 ms | – |
| obs-query-worker | since the final deploy | 47 | 425 ms | 695 ms | 738 ms | 0 |

- **Little traffic since the deploy**, mostly the canary and tests. These numbers are indications, not proof.
- **The median API time is higher** (401 → 585 ms). Unverified candidates:
  - the traffic mix: the canary's queries rather than people's;
  - the per-account in-flight slot, which costs DynamoDB writes to take and give back a slot (`services/compaction/query.py:1264-1293`).
  - To check after a few days of real use: Logs Insights on `/aws/lambda/obs-query-api`, as in the INFLIGHT_LIMITS note in docs/refactor-progress.md.
- **The worker runs of 133–300 s on 2026-10-17, 14:28–14:42 UTC** were the AWS verification's deliberately slow queries.
  - They ran before two fixes were deployed: a cancel check answered 403 and stopped the timeout watch (75ade31, 6f5db91).
  - Since the final deploy at 23:12 UTC, the slowest worker run is 0.74 s.

## Found and fixed in Phase 7

**1. The first visit to each page waited at least 300 ms** (f4276bc)
- *Cause:* per-page code went through React.lazy. On a first visit, React showed the loading placeholder, then held the page back at least 300 ms after it (react-dom 19.3, `globalMostRecentFallbackTime + 300`).
- *Effect:* +250–300 ms on the first load of almost every page.
- *Fix:* the opened page's code now loads at start-up, alongside the sign-in check, and renders at once. Moving to another page is a transition: the current page stays until the next is ready.

**2. Dashboards and SLOs used several "nows"** (7c34869)
- *Cause:* dashboard panels and SLO rows read the clock themselves.
- *Effect:* 2–3 different window ends per page.
- *Fix:* they use the page's one "now". The test was extended to both pages (`sharedNow.test.tsx`).

**3. Usage & cost waited for the account** (c543159)
- *Cause:* after the move into Settings, the estimate's queries started only once the account had loaded.
- *Effect:* first load went from 501 ms to 900 ms.
- *Fix:* the estimate renders beside the page and starts at once: 532 ms.

## Tests added

- **`services/web/src/crossPage.test.tsx`:**
  - A range picked on Logs carries to Traces, Metrics, Home, Kubernetes and Service map through the sidebar's own links: every query uses it, with one window end, and the link carries `range=`.
  - The range also carries to a fresh load.
  - After Refresh, the next page's window is no older than the Refresh.
- **`services/web/src/sharedNow.test.tsx`:** now also covers Dashboards and SLOs.
- All 131 web tests, lint and type-check pass.

## Still open

- **P7, F4, F5:** combine Home's overlapping card queries (about 20 → 14); split the largest modules; typed rows. No behaviour change but large diffs; not started.
- **INFLIGHT_LIMITS** (server, per account, 25 for every plan): revisit after a few days of real data.
- **obs-fastlane-failed alarm:** 2 messages in the fast-lane dead-letter queue since 2026-10-16 23:44 UTC. Unrelated to this work and not investigated.
- **Request counts** only fall with a cache, which was decided against.

## To re-run

All commands run in `services/web`.
1. Start the query engine: `python3 mock/engine_server.py 6`.
2. For each version, build it:
   `npx vite build --mode mock --outDir DIST`
3. Bundle its mock backend, and copy `mock/shot.jpg` into MOCKDIR:
   `npx esbuild mock/mockApi.ts --bundle --platform=node --format=esm --outfile=MOCKDIR/mock.mjs`
4. Serve each version, one port each (5301 and 5302):
   `node scripts/perf/serve.cjs MOCKDIR/mock.mjs DIST 5301`
5. Measure, with Playwright as installed on the machine (set `CHROMIUM` to a browser path if needed):
   `NODE_PATH=$(npm root -g) node scripts/perf/measure.cjs http://localhost:5301 http://localhost:5302 out.json 3`
