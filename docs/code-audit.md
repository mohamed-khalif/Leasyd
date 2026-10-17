# Code health, data consistency and security audit

Phases 2 and 3 of the review (`docs/refactor-progress.md`). It builds on the system map in `docs/ARCHITECTURE.md`
and doesn't repeat it.

Sources:
- Read-only audits of the frontend, backend and infra (run in parallel and merged). I re-checked the headline
  claims in code myself.
- Measurements taken in the local preview with a dev-only request log (`services/web/src/devRequests.ts`;
  it is compiled out of production, and the production bundle hash is unchanged).
- Production latency from CloudWatch.

No behaviour was changed. **Nothing in here has been fixed yet.**

Paths:
- Frontend paths are relative to `services/web/src`.
- Backend paths are relative to `services/compaction` unless a section says otherwise.

Ratings:
- Severity: High, Medium (Med), Low.
- Impact and effort: H, M, L / S, M, L.

Contents:
1. Measurements
2. Data consistency and freshness (Phase 2)
3. Genuine bugs
4. Security and resource limits (backend and infra)
5. Frontend quality and performance
6. Query construction
7. Tests
8. Ranked plan (impact vs effort)

---

## 1. Measurements

**How:**
- `npm run mock` plus `mock/engine_server.py`, with Playwright driving each route.
- Each page: a fresh load, then one press of Refresh.
- The 15-minute default range.
- No React StrictMode is used (`main.tsx` renders `<App/>` directly). So **nothing below is a dev double-invocation**: the counts are what production sends.

**Limits of this method:**
- Response times and sizes are the mock's, so they show proportions, not production numbers.
- Production numbers are in the second table.

| Page | Requests on load | Query POSTs | Same query twice | Distinct "now"s (end times) | Requests per Refresh | Payload (mock) |
|---|---|---|---|---|---|---|
| Home `#/` | **23** | **20** | 0 | **6** | 21 | 245 KB |
| Logs | 5 | 3 | 0 | 1 | 3 | 83 KB |
| Traces | 5 | 3 | 0 | 1 | 3 | 168 KB |
| Metrics | 5 | 3 | 0 | 1 | 3 | 3 KB |
| Service map | 4 | 2 | 0 | 1 | 2 | 2 KB |
| Web (RUM) | 10 | 8 | 0 | 1 | 8 | 4 KB |
| Lambda | 4 | 2 | 0 | 1 | 2 | 2 KB |
| Kubernetes | 8 | 6 | 0 | 1 | 6 | 31 KB |
| GPUs | 7 | 4 | 0 | 1 | 5 | 9 KB |
| Dashboards (built-in Service overview) | **18** | **15** | 0 | **2** | 16 | 42 KB |
| Query Builder | 4 | 2 | 0 | 1 | 2 | 10 KB |
| SQL | 2 | 0 | 0 | – | 0 | 1 KB |
| Alerts | 6 | 0 | 0 | – | 4 | 6 KB |
| Synthetics | 6 | 3 | 0 | 1 | 4 | 27 KB |
| SLOs (2 SLOs) | 10 | 6 | 3 same-shape | 2 | 8 | 5 KB |
| Leasyd AI | 11 | 7 | 0 | – | 8 | 9 KB |
| Get started | 6 | 3 | **1** (`account.get`) | 1 | 0 | 3 KB |
| Drop rules | 9 | 4 | **1** (`account.get`) | 2 | 3 | 3 KB |
| Settings | 4 | 0 | **1** (`account.get`) | – | 1 | 2 KB |
| Usage | 6 | 3 | 0 | 1 | 4 | 2 KB |

Every page also sends `GET /v1/app/me` and the trial banner's `GET /v1/app/account`, which is 2 of the requests on load.

**Navigation:**

| Path | Result |
|---|---|
| Kubernetes → GPUs → Kubernetes → GPUs | Each visit re-runs its discovery query, which is the same SQL on both pages. Each visit re-sends every query (5–6). |
| Home → Traces → Home | 24 requests. Returning to Home re-sends all 20 queries, because nothing is reused. |

**Production (CloudWatch, last 7 days):**

| Function | Invocations | p50 | p95 | p99 |
|---|---|---|---|---|
| `obs-query-api` | 1,673 | 401 ms | 2,729 ms | 4,429 ms |
| `obs-query-worker` | 1,259 | 478 ms | 1,000 ms | 3,158 ms |
| API Gateway, all routes | – | 41 ms | 115 ms | – |

**Reading:**
- **Few exact duplicates within one page load.** The waste is elsewhere:
  - the Home page's 20-query fan-out on every load and every Refresh;
  - re-fetching everything on every navigation and visit, discovery included;
  - repeated REST loads (`account.get`, `checks.list`) across components;
  - per-row fan-out on SLOs.
- **Home asks 20 queries over 6 different windows** in a single load: each panel computes its own "now".
- **Queries are expensive.** At p95 = 2.7 s on the API Lambda (2 GB), the Home page costs about 20 Lambda invocations per view.

---

## 2. Data consistency and freshness (Phase 2)

### 2.1 Answers to the ten questions

1. **Hook overlap.**
   - `useQuery`, `useSql` and `useRows` are the same 12-line effect three times (key-based effect, `live` flag, `{data, error, loading}`). `useHint` is the same again.
   - They agree on change of key: each keeps the old data while loading.
   - They disagree on **display**:
     - `Loads` (`components/Panel.tsx:20`) ignores `loading`, so JSON and SQL panels show the **stale data** while refreshing.
     - `loads()` (`infra/useInfra.ts:63`) maps loading to "no data", so K8s and GPU panels **flash a skeleton**.
   - Other differences:
     - `useRows` starts `loading:true` even with no query.
     - A query that becomes `null` while in flight leaves `loading:true` forever (e.g. Traces `heat` after switching to RED).
   - Two custom variants add their own behaviour:
     - Dashboards `usePanelData` **clears** to a skeleton on every change.
     - QueryBuilder `useResults` has a per-key cache and the stuck-loading bug (B1).
2. **Key consistency.**
   - Most keys include the range and the tick.
   - **These ignore Refresh** (never refresh until remount):
     - `DurationCompare` (Traces and Lambda span panels)
     - Check run panel, Web visit drawer, Lambda invocation logs
     - Dashboard `get`, AI list, Maintenance checks
     - Alerts and SLO forms (pass 0), Settings GPU prices, trial banner
   - **"Now" is computed per component** (25 `rangeWindow` call sites; one per `usePanel` on Home), so panels on one page cover slightly different windows. Measured: **6 distinct end times on Home, 2 on Dashboards.**
   - Relative ranges are resolved at render, in effects, or on click, depending on the page.
3. **Refresh.**
   - It is global, but **manual only**: there is no auto-refresh anywhere.
   - Panels on one page refresh together, except the views listed in 2.
   - Page-local timers (Get started, AI, check "Run now", Stripe return) run independently.
   - No page is "live" while another is stale. They are all stale until Refresh.
4. **Time range and filters.**
   - The range is global (App state) and persists in localStorage.
   - It is **not in the URL**: a shared link opens with the reader's own range, and Back doesn't restore it.
   - Service, filters, tabs and views are per-page state, read from the URL once at mount and never written back. The exception is Query Builder, which writes its state with `replaceState`.
   - Moving to the same page with new `?service=` is ignored (the page isn't remounted). Only Kubernetes, GPUs and Lambda are keyed.
5. **Request lifecycle.**
   - **No `AbortController` anywhere.** "Ignore late answers" is all there is.
   - `runQuery` keeps polling a job every 2 s **for up to 6 minutes after the user has left the page**.
   - Dashboards' 6-slot limiter keeps slots taken by abandoned jobs, so new panels queue behind them.
   - **Backend:**
     - There is no cancel path.
     - A query that crosses 20 s is **started again as a job while the first run keeps going** (query.py:805-816). Up to 2 × 300 s of worker time per request.
6. **Auth during long operations.**
   - `idToken()` calls Cognito `getSession` per request, which refreshes the 15-minute ID token with the 30-day refresh token. A 6-minute poll is fine.
   - Gaps:
     - Parallel requests each refresh separately. Home fires about 20 at once, so up to 20 concurrent refreshes, which risks Cognito throttling.
     - `getSession` errors of **any** kind (including network) read as "Your session has ended".
     - A 401 from the server is not retried and never returns the user to sign-in. The panel just shows text.
     - A network error at startup (`me()`) shows the sign-in screen.
7. **Error handling.**
   - Error messages are mapped in one place (`api.ts:59`), but:
     - there are no status-specific messages;
     - **Retry-After is never honoured**;
     - 429 and 503 aren't retried;
     - a single transient error while polling abandons a job that may still finish;
     - network errors show as "Failed to fetch".
   - **No retry button anywhere.** There is no error boundary, so a render exception blanks the app.
   - Several loaders **swallow errors as empty or healthy data** (see B3).
8. **Duplicate load.** See section 1. The K8s/GPU discovery query is re-run on every visit and duplicated between the two pages. On refresh it runs **in parallel with** the data queries, which use the previous hint, so "discovery first" holds only on the first load.
9. **Cross-page connectivity.**
   - Links carry identifiers in the URL: `?service=`, `#/traces/<id>`, `?gpu=`, `?pod=`, `#/lambda/<fn>`, `?template=`, `#/query?promql=`.
   - The time range comes from shared App state, so it carries over within a session, but **not in links**.
   - Discovery results aren't shared.
   - Service names are the common identifier, and pages agree on the `service` field.
   - The "service list" is computed four different ways: traces only, logs only, three signals, or SQL over spans. So a service can appear on one page and not another.
10. **Deployment freshness.**
    - `index.html`, `config.json` and `install/**` are `no-cache`. Hashed assets are `immutable`. CloudFront invalidates `/`, `/index.html` and `/config.json` on deploy. That part is correct.
    - **But** `--delete` removes the previous deploy's hashed assets. Today that is harmless (one bundle, already loaded), but it will break lazy-loaded chunks if code splitting is added.
    - There is no API versioning beyond `/v1` and no build-version check. An open tab runs the old bundle against the new API until reloaded, and nothing tells the user.

### 2.2 Inconsistency table

| # | Location | What diverges or goes stale | Sev | Proposed fix |
|---|---|---|---|---|
| C1 | `useQuery.ts`, `infra/useInfra.ts`, Dashboards `usePanelData`, QB `useResults` | Five copies of the fetch-state logic, with different behaviour on refresh: stale data, skeleton, or cleared | Med | One core hook (`useAsync(key, fn)`), with wrappers keeping today's signatures. Make the refresh display explicit (keep data and show a small "refreshing" state everywhere) |
| C2 | 25 × `rangeWindow` (Insights `usePanel`, pages, effects, handlers) | Each panel has its own "now": 6 windows on Home, 2 on Dashboards | Med | Resolve the window once per tick/range in a provider; every hook and effect reads it |
| C3 | DurationCompare, RunPanel, Web visit, Lambda logs, dashboard get, AI list, Maintenance checks, forms, Settings GPU prices, trial banner | Never refresh on Refresh | Low–Med | Include the tick (behaviour change: confirm), or document them as fixed-window views |
| C4 | Range in localStorage only | Not deep-linkable; shared links show the reader's range | Med | Mirror the range in the hash (`&range=1h` or `&from=&to=`); localStorage stays the default (behaviour change: confirm) |
| C5 | Per-page service and filter state, read once from the URL | Same-page links with new params are ignored; Back doesn't restore filters | Med | Read params on every hash change (or key pages by query string); write filters back with `replaceState` as Query Builder does |
| C6 | `useHint` (`infra/useInfra.ts:15-30`) | On refresh, data queries use the previous hint in parallel with the new discovery; discovery is repeated per page and visit | Low–Med | Share discovery through the cache (one per range+tick); reset the hint on key change, or keep the old one deliberately and document it |
| C7 | `api.ts:36-48`, `Dashboards.tsx:265-272` | Polling and queued requests continue after unmount; abandoned jobs hold limiter slots | High | AbortSignal through `runQuery` (fetch and poll sleep), aborted by the core hook's cleanup; release limiter slots on abort |
| C8 | `query.py:805-816` | At 20 s the query is restarted as a job while the first run continues: duplicate compute and double-charged units | High | Hand the running work to the job, or decide sync vs async up front (estimate from file bytes); never run twice |
| C9 | No cancel endpoint | Abandoned jobs run to completion (up to 300 s × up to 64 workers) | Med | `DELETE /v1/app/query/{job}` writes a cancel flag; workers check it between chunks; DuckDB `interrupt()` on a timer (see R1) |
| C10 | `auth.ts:70-82` | Concurrent refreshes; any error treated as "session ended"; no return to sign-in on 401 | Med | Single-flight `getSession`; separate "no session" from "refresh failed"; a 401 raises an app-level event that shows sign-in and keeps the hash |
| C11 | `api.ts:45, 59-62` | Retry-After ignored; 429/503 not retried; one bad poll kills the job wait; network error unmapped | Med | Typed `ApiError.kind`; bounded retry with Retry-After; tolerate 1–2 transient poll failures |
| C12 | `Shell.tsx:12`, DropRules, Settings, GetStarted | `account.get` fetched 2× per page on 3 pages, plus on every page by the banner | Low | Shared account loader (cache with in-flight de-duplication) |
| C13 | `checks.list` in Insights, Synthetics, Slos (×3), Maintenance (×2), Alerts, Dashboards (cached forever), AI | 8 places, 4 copies of `useChecks`, different refresh rules | Low–Med | One `useChecks()` on the shared loader |
| C14 | Service list built 4 ways (traces only, logs only, 3 signals, SQL spans) | Pages disagree on which services exist | Low | One `useServices(signals)` |
| C15 | `deploy-phaseW1.sh:46` `--delete` on assets; no build-version check | Old tabs can't load deleted chunks once code splitting exists; old bundles run silently against a new API | Low now, Med with splitting | Keep the previous deploy's assets (or expire them after N days); expose the build id in `config.json` and show "a new version is available" |

---

## 3. Genuine bugs

These are behaviour bugs. Fixing each is a deliberate behaviour change, so I'll confirm the list with you first.

| # | Bug | Where | Effect | Sev |
|---|---|---|---|---|
| B1 | Query Builder result stuck on "loading" | `pages/QueryBuilder.tsx:413-431` | Editing any query (or toggling hide) while another is in flight drops its answer and never re-asks. It clears only on Refresh or a range change. Easy to hit while typing. | Med |
| B2 | A 2 KB SQL query crashes the API Lambda | `query.py:447-473` (`check_sql` recursion) | Deep nesting causes a `RecursionError`, which is not mapped, so 503 and an alarm. Reproduced. Repeatable alarm noise. | Med |
| B3 | Failures shown as healthy or empty | `Ai.tsx:146` ("Nothing unusual" on error); `App.tsx:57` (any `me()` failure shows sign-in); GPU prices fall back to defaults silently (`useInfra.ts:58`, `Settings.tsx:196`), so costs are wrong; `DropRules.tsx:56` hides owner controls; `checks.list` errors become `[]`; Dashboards caches a failed `checks.list` for the session (`Dashboards.tsx:312`) | Users see "all clear" or wrong numbers instead of an error | Med |
| B4 | Line chart can throw for big results | `components/TimeSeries.tsx:27,29` (`Math.min(...xs)` / `Math.max(...)` over all points) | More than about 100k points (Query Builder allows 10k series) throws a RangeError. With no error boundary, **the app goes blank**. | Med |
| B5 | Malformed links blank the app | `App.tsx:86-87` (`decodeURIComponent` in render); `QueryBuilder.tsx:77-88, 104, 137` (unvalidated `?q=` state, `decimals` > 100) | A bad link causes a white screen (no error boundary) | Low–Med |
| B6 | Dashboard variable substitution | `Dashboards.tsx:254-259` `fillQuery` | (1) `$host` also rewrites the start of `$host_name`. (2) A variable name isn't regex-escaped. (3) A value used with `=` gets regex escaping (`a.b` becomes `a\.b`, which never matches). (4) Any leftover `$lowercase` token, even inside a user regex, becomes `.*`. | Low–Med |
| B7 | JSON-engine user errors become 503 | `query.py:261-263, 186-197` | Negative `search.limit` and RE2-invalid regexes (`(?=a)`) raise unmapped DuckDB errors, giving 503 and alarms. SQL maps the same kind of error to 400. | Low–Med |
| B8 | Job polling gives up on one transient error | `api.ts:45` | A single 503 while polling shows an error for a job that may still finish | Low–Med |
| B9 | Double-charged units on long queries | `query.py:806-816, 904-913` | A query that crosses 20 s is charged by the API and again by the job | Low–Med |
| B10 | MCP and AI queries aren't charged or limited by daily units | `query.py:728-755` | Usage through MCP and the AI SRE bypasses the daily allowance | Med |
| B11 | Lambda request-id search treats `_` and `%` as wildcards | `Lambda.tsx:128-133` | Wrong matches | Low |
| B12 | CSV export formula injection; object URL not revoked | `Sql.tsx:172-179` | Cells starting with `= + - @` from log data run as formulas in Excel | Med |
| B13 | Results silently truncated | Every SQL page except SQL ignores `truncated` | The service map or function lists may be incomplete with no notice | Low |
| B14 | A job can finish after its 504 and overwrite it; a failed async invoke leaves it "running" until the 504 | `query.py:941-982` | A confusing state for a later poll | Low |
| B15 | Smaller items | `CheckDetail.tsx:67` (60 s timer not cleared on unmount); SQL page Ctrl+Enter isn't blocked while running, so results race; ServiceMap can show "No data" while nodes load (`ServiceMap.tsx:98`); `Dashboards.decimals` > 100 throws | Minor glitches | Low |

---

## 4. Security and resource limits (backend and infra)

**Bottom line:** no way was found to read another tenant's data or local files.

- **Tenant isolation is enforced by IAM:**
  - Data is read through a tenant-tagged role restricted to `data/tenant=${tag}/*`.
  - The tenant comes only from the Cognito claim (immutable) or the API-key authorizer.
  - Job ids are regex-checked, and job keys are built from the authorizer's tenant.
- **The SQL lock-down held** against every escape tried (the probes were run against DuckDB 1.5.5):
  - no external access;
  - `allowed_paths` limited to exactly this run's files;
  - configuration locked;
  - replacement scans off.

**The real exposure is resource exhaustion.**

| # | Finding | Where | Scenario | Sev | Fix | Eff |
|---|---|---|---|---|---|---|
| R1 | No DuckDB statement timeout or interrupt; `threads` unset on SQL | `query.py:566-577`, `compact.py:212` | A recursive CTE or cartesian join passes the checker (verified) and holds a 3 GB worker for 300 s at a cost of 1 unit | **High** | Timer calling `con.interrupt()` (about 25 s sync, about 280 s jobs); `max_temp_directory_size`; set `threads` | S |
| R2 / I1 | No reserved concurrency anywhere; per-query fan-out up to 64 workers; 20 s restart doubles it | all templates; `query.py:806` | Free self-service tenants at 300 queries/min can exceed account concurrency and **throttle ingest for every customer** | **High** | Reserve concurrency for ingest and the authorizer; cap the query functions; per-tenant in-flight limit; no restart at 20 s (C8) | S–M |
| S1 | SQL checker: CTE names aren't scoped, so a CTE defined in one subquery whitelists that name anywhere (including a file path as a replacement scan) | `query.py:465-476` | Verified bypass of the checker. Still stopped by the worker lock-down, so it's a defence-in-depth hole. Also lets SQL reach the worker's temp tables, which skips the time filter on the tenant's own data. | Med | Scope CTEs per `SELECT_NODE`; refuse non-identifier table names; add to `test_sql.py` | S |
| S3 | `current_setting()` / `duckdb_settings()` allowed | `query.py` | Shows internal paths and settings (no data, no credentials) | Low | Deny-list in `check_sql` | S |
| R6 | Rate and daily limits fail open on DynamoDB errors | `query.py:933-938`, `ai/mcp.py:157` | Limits vanish when obs-tenants is throttled, which is exactly under stress | Med | Fail closed after N errors, or a local token bucket; alarm | S |
| R7 | JSON `services` and `in` lists uncapped; full `services` list logged | `query.py:215, 234`; `lookup.py:130` | One 10 MB body causes 100k DynamoDB queries and a huge log line | Med | Cap (50 / 2,000); log counts | S |
| R9 | SQL result has no byte cap; OOM retried once | `query.py:577-582, 1150-1171` | `repeat('x',1e8)` causes OOM, then a retry: double cost, 5xx | Low–Med | Measure bytes while fetching; no OOM retry on SQL | S |
| R10 | Job polls not rate-limited; each poll is a 2 GB Lambda plus DuckDB import | `query.py:790`, `65` | Cost noise | Low | Lazy-import DuckDB; poll limit | S |
| T1 | `AllowTestAssume=true` would let any account principal assume the tenant reader with **any** tenant tag; CloudFormation keeps a previous `true` silently | `infra/phase0-foundation.yaml:19-28` | Only if it was ever set to true (deployed value unverified) | Med (conditional) | Check the deployed stack; remove the parameter in production | S |
| T2 | `_results/*` not IAM-scoped per tenant | `phase0-foundation.yaml:375-380` | A bug in the query Lambdas could read others' stored answers (1-day TTL, no raw data) | Low | Read and write results through the tenant-reader session | M |
| A4 | Cognito: no MFA, no threat protection, `ALLOW_USER_PASSWORD_AUTH` enabled; no WAF | `infra/state.yaml:210, 231` | Credential stuffing is limited only by Cognito quotas | Med | Drop `USER_PASSWORD_AUTH` (the app uses SRP); offer TOTP MFA; WAF rate rules | S–M |
| I5 | No WAF on CloudFront or the API; the `execute-api` URL is directly reachable | infra | Unauthenticated floods hit the authorizer Lambda and DynamoDB | Med | WAF rate-based and managed rules | M |
| I2 | Dangling `obs-cache` DynamoDB grant | `phase0-foundation.yaml:380-385` | Grants access if a table with that name is ever created | Low | Remove | S |
| I3 | No security headers on `/v1/*` and `/config.json`; CSP `style-src 'unsafe-inline'` | `infra/phaseW1-web.yaml` | Hardening | Low | Attach the headers policy | S |
| I6 | `up.sh` doesn't deploy `obs-phaseAI` | `infra/up.sh` | After down/up the AI routes point at the old API id (503) until redeployed | Low–Med | Add to up/down | S |
| I7 | Deploy scripts pass `"$@"` after their own `--parameter-overrides`; the last one wins (verified with AWS CLI v1) | `deploy-phaseD1.sh:6,38` and 7 others | The documented `--parameter-overrides State=DISABLED` drops the script's own parameters | Low | Merge extra `K=V` into the script's own override array | S |
| Q1 | DuckDB imported on every `obs-query-api` cold start, including `/me` and job polls | `query.py:65` → `compact.py:18` | Slower cold starts | Low | Lazy import | S |
| Q5 | Logging | | Query text is **not** logged (good). The `services` list is logged unbounded (R7). AI error text can echo customer data (≤ 500 characters). | Low | Truncate | S |

Infra hygiene:
- About 15 copies of stack-output helpers.
- The `obs-state`/`obs-phaseU1` fallback is repeated 4 times.
- `--cli-binary-format` is inconsistent (T7 breaks on AWS CLI v1).
- Unset-array expansions fail on old bash.
- A personal email is the default `BudgetEmail`.

All Low. Details in ARCHITECTURE §9 and the infra audit notes.

---

## 5. Frontend quality and performance

### 5.1 Structure and duplication

| # | Finding | Fix | Imp | Eff |
|---|---|---|---|---|
| F1 | Shared helpers live in page files: Insights → Logs/Traces; Lambda → Traces; Alerts → Logs; CheckDetail ↔ Synthetics (**runtime import cycle**) | `lib/` for formatting, series and SQL/PromQL helpers; `pages/synthetics/shared.ts` | M | S |
| F2 | Duplicated helpers with **diverging behaviour**: `niceMax` ×2 (different axis tops); `stack`/`stackByGroup` ("unknown" vs "(none)"); `pct` ×4 (0–1 vs 0–100, string vs number); `memShort` ×2 (different units); `ago` ×2; legend expanders ×2; 7 colour palettes (Dashboards ignores the theme); `useWidth` vs an inline ResizeObserver; `rows()`, `ENTRY`, `q()` ×4; copy-to-clipboard ×2; tree builders with O(k²) spreads ×2 | One `lib/format.ts` with explicit names (`fmtPercent`, `fmtFraction`, `fmtBytesIEC`…); number formatting moves out of `time.ts`; shared chart utilities | M | S–M |
| F3 | Dead code: `components/Donut.tsx`, `isBuiltin`, `CHECK_PRICE_PER_THOUSAND`; about 30 exports used only in their own file; `ApiError` exported but never checked | Delete; un-export, or keep the ones meant for tests | L | S |
| F4 | Oversized: Synthetics 598 lines, Gpus 468, Traces 463, CheckDetail 460, Dashboards 448, QueryBuilder 444. Components: `QueryBuilder` 207 lines, `Settings` 178, `Web.Site` 149, `Logs` 147, `Traces.Explorer` 144, `CheckForm` 144 | Split along state hooks, query modules and sub-panels (seams listed in the audit notes); route table instead of App's if/else | M | M |
| F5 | Typing: row access through `Record<string, unknown>` with about 300 `String()`/`Number()` coercions (`String(null)` = "null"); `records(r) as unknown as T[]`; `loads()` fakes `data: {} as never`. No `any`, no `@ts-ignore`. | Typed hooks (`useSql<Row>`), small decoders (`num`, `str`), generic `Loads` | M | M |
| F6 | No ESLint, yet 25 `eslint-disable` comments | Add ESLint with react-hooks and import-boundary rules (Phase 6) | M | S |

### 5.2 Performance

| # | Finding | Fix | Imp | Eff |
|---|---|---|---|---|
| P1 | Heatmap: up to 10,000 cells (about 20k SVG nodes), each with its own handler; **every mouse move re-renders all cells** | Memoised cell layer, one SVG-level handler, hover overlay; canvas for very large N | H | M |
| P2 | `TimeSeries` re-sorts every series and rebuilds every path on every render, **including hover**; spreads every point (B4) | Memoise geometry; loop extents; hover in a child; cap rendered series (top 50 + "N more") | H | S |
| P3 | Bundle 766 kB / 221 kB gzip in one chunk: **app code ≈ 60%**, react-dom 27%, Cognito + polyfills 11% | Route-level `React.lazy` (fits the hash router); move cross-page helpers first; keep old chunks across deploys (C15). Initial chunk ≈ 110 kB gzip | H | M |
| P4 | Queries fire per keystroke with no debounce: Query Builder builder fields, Lambda request-id search, Logs custom group-by, Alerts thresholds; QB also rewrites the URL per keystroke | `useDebounced(…, 400)`; throttle URL sync | M | S |
| P5 | Un-memoised derived data: QB `series` and `barsOf` (O(T·S·P)); Logs patterns recomputed per keystroke (memo never hits); Treemap `squarify` (up to 2,000 tiles, O(n²)); Gpus analysis; Web `rows()` 2–3× per table | `useMemo`; Map-based lookups; cap the treemap at 200 tiles | M | S |
| P6 | Unvirtualised large lists: trace waterfall 2,000 rows; GPU/K8s tables up to 5,000; QB legend up to 10,000 | Small windowed list, or slice to 500 with "show more" | M | M |
| P7 | Home fires 20 queries (section 1), including two near-identical service-list queries | Combine the overlapping card queries; shared cache (Phase 4) | M | M |
| P8 | `ctx` object rebuilt every App render | `useMemo` (matters only once pages memoise) | L | S |

---

## 6. Query construction

**No UI or URL value reaches SQL as an identifier or as raw SQL**, apart from the SQL page, which is user SQL by design and protected by the backend lock-down. String values go through `q()`, which doubles `'` (correct for DuckDB, which has no backslash escapes outside `E''`). Values from the URL (`#/lambda/<fn>`, `?gpu=`, `?pod=`) are quoted. JSON queries pass values structurally (safe).

Weak points:
- four copies of `q()`;
- no LIKE escaper (B11);
- Dashboard variable substitution (B6);
- Query Builder accepts unvalidated link state (B5);
- SQL is built inline in three pages (ServiceMap, Web, Lambda) while K8s/GPU use a builder module.

**Proposal:** `lib/sql.ts` and `lib/promql.ts`, the only places query text is assembled from values.
- **SQL:** a tagged `sql\`\`` template whose values must be `str()`, `num()` (finite only), `like()` (escapes `\ % _` with `ESCAPE '\'`), `ident()` (allow-list), `attr()` or `interval()`.
- **PromQL:** `promString`, `promLabelName`, `matcher` and `substituteVars`. `substituteVars` escapes the name, uses a word boundary, and escapes per context (regex for `=~`, plain value for `=`).
- **Migration:** `infra/queries.ts` (the contract test keeps working because builders still return strings); the inline SQL in ServiceMap, Web and Lambda; `toPromQL` and `selector` in Query Builder; `fillQuery` in Dashboards; Ai insights.
- **Enforcement:** an ESLint rule (or a grep check in CI) bans query-text construction in `pages/`.

---

## 7. Tests

| Area | Today | Gap |
|---|---|---|
| Frontend | One file (`infra/analysis.test.ts`). No tests of hooks, `api.ts`, polling, auth, builders or pages. | Everything in Phase 5 |
| Backend query path | 232 tests in compaction (engine, isolation, jobs, SQL lock-down, K8s/GPU contracts) | CTE-scope bypass (S1), deep nesting (B2), runaway SQL interrupt (R1), fail-open limits (R6), units on direct invokes (B10), no double charge (B9), negative limit / RE2 regex (B7), list caps (R7), oversize SQL result (R9), job invoke failure and finish-after-504 (B14), API-key `GET /v1/query/{job}` |
| Infra | cfn-lint only | A check that every function has a reserved-concurrency decision, no dangling grants, and `AllowTestAssume` false |

**Tooling for the Phase 5 safety net:**

| Kind of test | Dependencies | Notes |
|---|---|---|
| `api.ts` error mapping | None | Node 22's `node:test` with `mock.timers` and the global `fetch`/`AbortController`; `scripts/test.mjs` already bundles `*.test.ts` with esbuild |
| The 20 s → job → poll flow with fake timers | None | |
| Builders, URL-state parsing | None | |
| Hook state transitions | None, if extracted | Once the hook logic is a pure reducer |
| Real hook tests, the QB regression, one smoke test per page | **A DOM** | Smallest addition: `happy-dom` + `@happy-dom/global-registrator` (2 dev dependencies), with React's own `act`. `@testing-library/react` is optional. |

`esbuild` is used by the test script but not declared; declare it.

---

## 8. Ranked plan (impact vs effort)

| Rank | Item | Refs | Impact | Effort |
|---|---|---|---|---|
| 1 | Reserved concurrency (protect ingest and the authorizer; cap query functions) and a DuckDB interrupt timer on SQL | R2/I1, R1 | High | S |
| 2 | Error boundary (top-level and per route); safe hash decode; validate QB link state; fix the TimeSeries spread | B4, B5 | High | S |
| 3 | Map `RecursionError`/`duckdb.Error` to 400; clamp limits; RE2-valid regexes; scope the CTE check | B2, B7, S1 | Med | S |
| 4 | AbortSignal through `runQuery` and the core hook; stop polling on unmount; free limiter slots | C7 | High | M |
| 5 | No restart at the 20 s deadline (hand over, or decide async up front); charge once; backend cancel | C8, B9, C9 | High | M |
| 6 | Stop showing failures as empty or healthy | B3 | Med | S |
| 7 | One core fetch hook (collapses C1), one window per tick (C2), single-flight token refresh, 401 → sign-in, Retry-After / transient-poll tolerance, retry buttons | C1, C2, C10, C11 | Med | M |
| 8 | Fix the QB stuck-loading bug (falls out of 7) | B1 | Med | S |
| 9 | Charge and limit MCP/AI queries; cap `services`/`in`; fail closed on limit errors | B10, R7, R6 | Med | S |
| 10 | Memoise and lighten the Heatmap and TimeSeries; debounce per-keystroke queries | P1, P2, P4 | High | M |
| 11 | Code splitting by route, keeping old chunks on deploy and adding a build-version check | P3, C15 | High | M |
| 12 | `lib/sql.ts` and `lib/promql.ts`; fix `fillQuery`; LIKE escaping; CSV export guard | §6, B6, B11, B12 | Med | M |
| 13 | Range and filters in the URL (deep links) | C4, C5 | Med | M |
| 14 | Shared account and checks loaders; one service-list hook; shared discovery | C12–C14, C6 | Low–Med | S |
| 15 | Split oversized modules, `lib/` for cross-page helpers (breaks the cycle), consolidate formatters, delete dead code, typed rows | F1–F5 | Med | M |
| 16 | Security hygiene: check `AllowTestAssume`, drop `USER_PASSWORD_AUTH`, MFA option, WAF, headers on `/v1` and `/config.json`, remove the `obs-cache` grant, up.sh covering phaseAI, deploy-script overrides | T1, A4, I2–I7 | Med | S–M |

**Items 1, 3, 6, 9 and 16 change behaviour** (limits, error messages, security settings). Each needs your yes before it's implemented, as do the bug fixes in section 3.
