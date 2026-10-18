# Leasyd architecture

A map of the system, written for the code-health review (`docs/refactor-progress.md`).

**Two dates in this file.** Section 0 is the web app as it is now (2026-10-18, after Phases 4–7). Sections 1–9 are the
audit as found on 2026-10-17, before the clean-up; they are kept as the "before" picture, and their `file:line`
references point at that code (commit d2a42dc). The backend sections (8, 9) are still accurate except where section 0
says otherwise. Measured before/after results: `docs/phase7-report.md`.
Facts carry `file:line` references; paths are relative to the repository root unless a section says otherwise.
Findings that need action are collected in `docs/code-audit.md` (Phase 3). This file describes, it doesn't judge,
except where a "Flag" column or note says so.

Contents:
0. The web app now (after the clean-up)
1. Claims checked
2. Repository layout
3. Frontend (services/web)
4. Data-fetch call sites
5. Pages and the queries they issue
6. Global state
7. Frontend module graph
8. Backend
9. Infrastructure and hosting

---

## 0. The web app now (after the clean-up)

Paths are relative to `services/web/src`. About 11,200 lines of TS/TSX plus 850 lines of CSS. Per-folder notes:
`services/web/src/README.md`.

### 0.1 How a page gets its data

```
App (hash router, range, Refresh tick, the page's one "now")
 └─ page ── useQuery / useSql / useRows / useHint ──┐
                                                    ├─ useLoad (useQuery.ts): runs on key change, aborts the old run
 └─ useAccount / useCheckList (one loader each) ───┘
        └─ api.ts  query/sql/promql → runQuery → limited (limiter.ts, 6 at once) → fetch POST /v1/app/query
                                                   202 + job id → poll every 2 s; abort → DELETE /v1/app/query/{job}
```

- **One core hook.** `useLoad(run, key)` (`useQuery.ts`) runs `run(signal)` whenever `key` changes, keeps the last
  data while the next loads, and aborts on a new key or unmount, so a late answer is never shown. `useQuery`, `useSql`
  (`useQuery.ts`), `useRows`, `useHint` (`infra/useInfra.ts`), `useAccount` (`useAccount.ts`) and `useCheckList`
  (`checkList.tsx`) are thin wrappers. Dashboards' panels keep their own effect and abort the same way; Query Builder's
  results abort only when the page closes (as decided in step 6.2).
- **Requests.** Everything goes through `api.ts` (lint: `fetch` only there and in `config.ts`). Typed `ApiError`.
  While a job runs, a poll that fails in passing (502/503/504 or network) is tried again. Aborting a query that
  became a job sends `DELETE /v1/app/query/{job}`.
- **At most 6 queries in flight per page** (`limiter.ts`, `MAX_IN_FLIGHT`). The rest wait in order; an aborted
  query leaves the queue or frees its place. The server has its own per-account limit (`INFLIGHT_LIMITS`,
  `services/compaction/query.py`, 25 for every plan for now).
- **No cache** (decision: `docs/caching-decision.md`). Every page visit and Refresh asks again.

### 0.2 Time range, "now", Refresh and links

- **Range:** in `App` state, saved to localStorage `leasyd.range`, and mirrored in the link as `?range=` (`15m`…`30d`,
  or `c<from>-<to>` for a custom range). A link with a range opens with it; Back and links switch to theirs.
- **One "now" per page:** `ctx.now`, set when the range changes, on Refresh, and on opening a page. Every query on
  the page ends there: `useRangeWindow(ctx)` (`useNow.ts`) for range windows; views with their own window (SLO
  period, a trace's 30-day lookup, usage's 24 hours) use `useNow(...)` or `ctx.now` explicitly. Exceptions, on
  purpose: Kubernetes/GPU discovery reads the last 15 minutes of the window; each drop rule preview reads its own clock.
- **Refresh** bumps `ctx.tick` and takes a new now. There is no auto-refresh anywhere.
- **Filters in the link:** `useHashParam` / `useHashSearch` (`hashParams.ts`) keep a page's service, filters and
  search in the link (`replaceState`), so links and Back restore them. Typing waits 400 ms (`useDebounced.ts`,
  `TYPING_MS`); Enter or Run asks at once.

### 0.3 Pages and code loading

- **One code chunk per page.** `App.tsx` `splitPage(...)`: the page a link opens is downloaded at start-up next to
  the sign-in check (`preloadPage`) and renders without a fallback; moving to another page is a React transition,
  so the current page stays until the next one's code is there. (Through plain `React.lazy` each first visit waited
  at least 300 ms: `docs/phase7-report.md`.) First download: 109 kB gzip (was 224).
- **Pages don't import pages** (lint). Shared code is in `lib/`, `components/`, `infra/`, or a page folder
  (`pages/synthetics/shared.tsx`, `pages/settings/`). Synthetics opens CheckDetail and Maintenance.
- **Settings** (`pages/Settings.tsx`, `pages/settings/`): a menu and one page per topic at `#/settings/<page>`
  (`settings/menu.ts`); `#/usage` opens Settings › Usage & cost.
- **Long lists** draw 500 rows at a time with "Show more" (`components/Capped.tsx`); waterfall search,
  expand/collapse all and CSV export work on everything.
- **Errors are shown as errors**, never as "all clear", empty or 0 (`failures.test.tsx`). An `ErrorBoundary` around
  each page catches render errors; a bad link can't blank the app (`links.test.tsx`).

### 0.4 Formatting and rules

- **All number, date and size formatting** is in `lib/format.ts`, spec `docs/formatting.md`. Lint forbids
  `toFixed`, `toPrecision`, `toLocale*` and `Intl` in `pages/` and `components/`.
- **SQL text** from values: `lib/sql.ts` `sqlString` (one helper; the engine checks every query too).
- **Lint** (`eslint.config.js`): the rules above, React hooks rules, `fetch` only in `api.ts`/`config.ts`.
- **Tests:** `npm test` (node:test bundled by esbuild, happy-dom, the mock backend `mock/mockApi.ts`): one smoke test
  per page, failure paths, shared now, range in links, cross-page range and Refresh, limiter, abort, debounce, row
  cap, formatting, Settings. 131 tests on 2026-10-18.

### 0.5 What changed from sections 3–7

| Then (2026-10-17) | Now |
|---|---|
| Five copies of fetch-state logic; no abort | One `useLoad`; abort on key change and unmount; jobs cancelled |
| Up to 7 "nows" on one page | One `ctx.now` per page |
| Range only in localStorage; filters read once | Range and filters in the link, read on every change |
| Home sent 22 requests at once | At most 6 queries in flight per page |
| 8 check-list loaders, `account.get` twice per page | One `useCheckList`, one `useAccount` (no cache) |
| Formatting in pages and `time.ts` | `lib/format.ts` + lint rule |
| SQL quoting copied 4 times | `lib/sql.ts` |
| One 224 kB (gzip) bundle | A chunk per page, the opened one preloaded; 109 kB first download |
| Pages importing pages; Synthetics ↔ CheckDetail cycle | Shared code moved out; lint rule; no cycle |
| No ESLint; 1 test file | ESLint; 131 tests |
| Usage & Cost its own page | Settings › Usage & cost |

---

## 1. Claims checked

The review brief listed seven claims about the frontend. Here is what the code says.

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | React 19 + TypeScript + Vite SPA, served from S3 + CloudFront | True | `services/web/package.json`; `infra/phaseW1-web.yaml` |
| 2 | Runtime deps are only react, react-dom and amazon-cognito-identity-js | True | `package.json`. Dev-only: vite 7, typescript 5.8, @vitejs/plugin-react. `scripts/test.mjs` and `scripts/export-infra-queries.mjs` import `esbuild` without declaring it (it comes in through vite). |
| 3 | No router; hash routes in App.tsx | True | `useHash()` `App.tsx:32-40`; if/else chain `App.tsx:64-133`. Only Gpus, Kubernetes and Lambda are keyed, so they remount on param change. Other pages read URL params once, at mount. |
| 4 | `src/api.ts` wraps fetch, adds the Cognito token, maps errors | True, with gaps | `call()` `api.ts:22-31` and `raw()` `api.ts:50-57` are near copies. Network errors (fetch `TypeError`) are not mapped. `signup()` (`api.ts:79-83`) uses fetch directly, with no token, which is correct for a public route. |
| 5 | Every chart/table sends SQL, PromQL or JSON to `POST /v1/app/query` | Mostly true | All `query`/`sql`/`promql`/`promqlAt` go through `runQuery` (`api.ts:36-48`). Data shown in charts or tables also comes from other endpoints: `POST /v1/app/alerts/rules/preview` (Alerts "Right now" table), `GET /v1/app/account/meters` (Usage, Drop rules), check screenshots, and the AI conversations. |
| 6 | >~20 s → job id; client polls every 2 s up to 6 min | True | Server: `SYNC_DEADLINE_S = 20` (`services/compaction/query.py:763`). Client: `api.ts:39-45`. |
| 7 | useQuery, useSql, useRows run on key change and ignore late responses; K8s/GPU run discovery first | True, with a caveat | All four hooks (`useQuery.ts:7-36`, `infra/useInfra.ts:15-53`) use a `live` flag. Discovery comes first **only on first load**: `useHint` never goes back to `undefined`, so on refresh or range change the data queries run at once with the previous hint, alongside the new discovery. |
| 8 | There is no caching or de-duplication of queries | **False** | Several small caches exist, but no shared one. (a) `Dashboards.tsx:312-316` keeps a module-level promise of check names for the page's lifetime; a failure is cached too, as an empty map. (b) `QueryBuilder.tsx:413-431` `useResults` memoises PromQL results per key in component state and never evicts them. (c) `DropRules.tsx:61-65` skips previews already computed. (d) `Dashboards.tsx:265-272` is a module-level limiter of 6 concurrent panel queries (a limiter, not a cache). (e) `config.ts` and the Cognito pool are memoised. There is no cross-component de-duplication: identical concurrent queries are each sent. |

---

## 2. Repository layout

| Path | Owns |
|---|---|
| `services/web/` | The SPA (React + Vite), its mock backend (`mock/`), build scripts (`scripts/`) |
| `services/compaction/` | Query engine (JSON, PromQL, SQL), query HTTP API and jobs, index lookup, fast-lane indexer, compaction to Parquet |
| `services/ingest/` | OTLP/HTTP ingest, API-key authorizer, RUM, CloudWatch metric streams, drop rules |
| `services/tenants/` | Tenant control plane (`admin.py`), self-service account and sign-up (`account.py`), Stripe billing, emails |
| `services/alerts/` | Alert channels and rules, SLO burn alerts, dashboards API |
| `services/synthetics/` | HTTP and browser checks, maintenance windows, SLO definitions, SSRF guard |
| `services/ai/` | AI SRE (`sre.py`) and MCP server (`mcp.py`) |
| `services/canary/` | End-to-end freshness and route canary |
| `services/demo/`, `services/loadgen/` | Demo telemetry generator (used by tests and the local preview); load tests |
| `infra/` | CloudFormation templates (`*.yaml`), deploy scripts (`deploy-*.sh`), `up.sh`/`down.sh`, GPU lab, IAM policies |
| `examples/` | Customer-facing install material (collector, Kubernetes, EC2, language examples) |
| `site/` | Marketing site (www) |
| `docs/` | Design notes, STATUS, this file |

`services/*/build/` directories are vendored, gitignored deploy bundles; they are not source.

---

## 3. Frontend (`services/web/src`)

10,431 lines of TS/TSX plus 812 lines of CSS.

| Module | Lines | Owns |
|---|---|---|
| `main.tsx` | 11 | Theme, `loadConfig()`, render `<App/>` |
| `config.ts` | 19 | Fetches `/config.json` (no-store) once; `getConfig()` |
| `App.tsx` | 146 | Auth gate (`me()`), hash router, global `range` and `tick`, `Ctx` type, `leasyd.range` in localStorage |
| `Shell.tsx` | 178 | Sidebar nav, top bar (theme, range picker, Refresh), trial banner, `CONTACT_EMAIL` |
| `api.ts` | 272 | All HTTP: `call`/`raw`/`runQuery` (job polling); typed API objects (account, checks, windows, slos, alerts, dashboards, ai); `query`, `sql`, `promql`, `promqlAt`; helpers `records`, `notExcluded`, `rumSnippet` |
| `auth.ts` | 102 | Cognito SRP sign-in, new-password challenge, forgot/reset, sign-out, `idToken()` (refreshes through `getSession`) |
| `time.ts` | 92 | `Range`, presets, custom ranges, `rangeWindow` (computes "now"), `bucketSeconds`, retention; **also** number formatters `fmtNum`, `fmtMs`, `fmtUnit` |
| `useQuery.ts` | 36 | `useQuery` (JSON), `useSql` |
| `builtinDashboards.ts` | 336 | Read-only built-in PromQL dashboards |
| `pricing.ts`, `icons.tsx`, `styles.css` | | List prices; SVG icons; all CSS |
| `components/Charts.tsx` | 359 | StackedBars, Heatmap, Treemap, StatusStrip, ViewsSidebar, Tabs, ColumnPicker, Section, Drawer, Card, `useWidth`, `niceMax` |
| `components/TimeSeries.tsx` | 109 | Line/area chart (its own ResizeObserver and `niceMax`) |
| `components/Panel.tsx`, `RankTable.tsx`, `Stat.tsx` | | Panel frame and `Loads` wrapper; ranked table; stat and gauge |
| `components/Donut.tsx` | 28 | **No importers (dead)** |
| `infra/` | | Kubernetes and GPU: `model.ts` (metric names, XIDs, prices), `queries.ts` (every K8s/GPU SQL as a pure builder), `useInfra.ts` (`useHint`, `useRows`, `useGpuPrices`), `analysis.ts` (+ test), `format.ts`, `alertTemplates.ts` |
| `pages/` | | 25 pages (section 5). Largest: Synthetics 598, Gpus 468, Traces 463, CheckDetail 460, Dashboards 448, QueryBuilder 444 |
| `mock/` | | `npm run mock`: Vite middleware answering `/v1/app/*`, simulating the 202/job flow, forwarding K8s/GPU SQL to `engine_server.py` (the real engine on moto) |

Tooling:
- Type-check: `tsc -b` (part of `npm run build`).
- Tests: `npm test` runs `scripts/test.mjs`, which bundles `*.test.ts` with esbuild and runs node:test. The only test file is `infra/analysis.test.ts`.
- There is **no ESLint**, although the code has 25 `eslint-disable` comments.

---

## 4. Data-fetch call sites

Paths in this section are relative to `services/web/src`.

**Legend**
- **Time:** R = `useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick])` in that component; E = `rangeWindow` inside an effect; H = in an event handler; or a fixed window as stated.
- **Refresh:** "tick" means the key includes `ctx.tick` (the Refresh button).
- **Guard:** late responses are ignored.
- **Mechanism:** hook = useQuery/useSql/useRows; direct = a promise in `useEffect`; handler = an event handler.
- **Endpoint:** `/v1/app/query` unless stated.

### 4.1 Telemetry queries (JSON, SQL, PromQL)

| Site | Mechanism | Type | Time | Refresh | Guard | Notes |
|---|---|---|---|---|---|---|
| `Insights.tsx:54-62, 141-144, 193, 211-213, 246-247, 269, 271` | useQuery via `usePanel` (:20-23) | JSON ×19 | R **per panel** | tick | yes | Each panel has its own `useMemo`, so its own "now" |
| `Logs.tsx:72-78` | useQuery | JSON ×2–6 by tab | R | tick | yes | |
| `Logs.tsx:79` | useQuery | JSON | R | `range.key+tick` | yes | Service drop-down |
| `Traces.tsx:74-81` | useQuery | JSON ×4–5 | R | tick | yes | |
| `Traces.tsx:218-219` | useQuery | JSON ×2 | now−30 d | tick | yes | Trace waterfall: ignores the range |
| `Traces.tsx:395-396` | useQuery | JSON ×2 | span ±30 min | `span_id` | yes | `DurationCompare`, also used by Lambda; **no tick** |
| `Metrics.tsx:46-50, 157-160, 245, 249` | useQuery | JSON ×3 list, ×5 detail | R (two components) | tick | yes | |
| `ServiceMap.tsx:75, 193-198` | useSql | SQL ×2–4 | R (two components) | tick | yes | SQL built in the page |
| `Web.tsx:37-101` | useSql | SQL ×8 | R | `range.key+tick` | yes | SQL built in the page |
| `Web.tsx:226` | useSql | SQL | w | `session` | yes | Visit drawer; **no tick** |
| `Lambda.tsx:40-43, 113-129` | useSql | SQL ×2 list, ×4 function | R (two components) | tick | yes | SQL built in the page; search escapes `'` only |
| `Lambda.tsx:224` | useSql | SQL | span ±5 min | `span_id+tab` | yes | **no tick** |
| `Kubernetes.tsx:24` | `useHint` | SQL discovery | last 15 min of R | tick | yes | Same SQL as Gpus:26 |
| `Kubernetes.tsx:31-32, 94-96, 214, 289, 328-330` | useRows | SQL (`infra/queries.ts`) | R | tick (+filters, pod id) | yes | workloads and events issued twice (Overview and own tab) |
| `Gpus.tsx:26` | `useHint` | SQL discovery | last 15 min of R | tick | yes | |
| `Gpus.tsx:32, 75-76, 186-187, 282-283, 360, 415-416` | useRows | SQL | R | tick | yes | `gpuHealth` and `gpuXids` issued twice; four queries embed the same `gpuBuckets` CTE |
| `Dashboards.tsx:206-211` | useQuery | JSON ×4 | R | tick | yes | Variable values: services of 3 signals and field values |
| `Dashboards.tsx:276-308` | custom `usePanelData` | PromQL per panel query | **E per panel** | `[queries, vars, range, tick]` | yes | Clears to null on key change; 6-slot module queue never cancelled; `Promise.all`, so one failed query fails the panel |
| `QueryBuilder.tsx:100` | useQuery | JSON | R | `range.key+tick` | yes | Metric list |
| `QueryBuilder.tsx:413-431` | custom `useResults` | PromQL per query | R | key per text, range, step, tick | **partial** | Per-key cache never evicted; **stuck-loading bug** (see note below the table) |
| `Sql.tsx:65-77` | handler | SQL (`background`, `onWait`) | H | manual | no | The only caller of async jobs and of `onWait` |
| `Alerts.tsx:280` | useQuery | JSON | R | tick | yes | History: logs of service `alerts` |
| `Alerts.tsx:305-315` | handler and mount effect | PromQL | now−3 h | Preview button | **no** | |
| `Alerts.tsx:316-322` | direct | REST `POST /v1/app/alerts/rules/preview` | server | rule edits | yes | |
| `Synthetics.tsx:71-73` | useQuery | JSON ×3 | R | tick (+exclusions) | yes | |
| `CheckDetail.tsx:57, 120-127` | useQuery | JSON ×8 | R / previous window | tick, nonce | yes | `prev` repeats `tally` over the previous window |
| `CheckDetail.tsx:287` | useQuery | JSON | run ±1 h | run id | yes | **no tick** |
| `Slos.tsx:69-83, 157-159` | useQuery | JSON, 2–4 **per SLO row** | `now` memo on tick; SLO window | tick | yes | N+1 fan-out on the list; ignores the global range |
| `Ai.tsx:105-148` | direct `Promise.all` | PromQL ×7 + `checks.list` | E | range, tick | yes | Live insights; PromQL built in the page |
| `GetStarted.tsx:59-73` | direct + `setInterval` 20 s | JSON ×3 | now−15 min | poll | yes | Bypasses the hooks |
| `DropRules.tsx:61-74` | function from effect and button | JSON ×2 per rule | now−24 h | rule key | no | Bypasses the hooks; local result cache |
| `Usage.tsx:16-22` | useQuery | JSON ×3 | now−24 h memo on tick | tick | yes | Ignores the global range |

**The Query Builder stuck-loading bug (`QueryBuilder.tsx:413-431`):**
1. One effect starts every missing key, and its cleanup sets one shared `live=false`.
2. If query A is in flight and any key changes (an edit to query B, which happens on every keystroke, or toggling "hidden"), the cleanup drops A's answer.
3. The new run then skips A, because `state[A]` already exists with `loading: true`.
4. So A shows "loading" until the next refresh or range change.

### 4.2 REST loaders (`/v1/app/*` CRUD and account)

| Data | Sites | Mechanism | Refresh | Guard |
|---|---|---|---|---|
| `me` | `App.tsx:56-57` | direct | mount, sign-in | no |
| `account.get` | `Shell.tsx:12` (trial banner), `GetStarted.tsx:46-56` (10 s poll), `DropRules.tsx:54`, `Settings.tsx:24-35` | direct | mount / poll / tick | GetStarted only |
| `account.meters` | `DropRules.tsx:54`, `Usage.tsx:28` | direct | tick | no |
| `account.gpuPrices` | `infra/useInfra.ts:56-60` (GPUs), `Settings.tsx:196` | direct | tick / mount | no |
| `checks.list` | `Insights.tsx:42`, `Synthetics.tsx:52`, `Slos.tsx:29`, `Maintenance.tsx:20` (four `useChecks` copies), `Alerts.tsx:37`, `Dashboards.tsx:312` (cached), `Ai.tsx:105` | direct | tick, mount or never | Insights and Synthetics only |
| `checks.get` | `Synthetics.tsx:155`, `CheckDetail.tsx:51` | direct | id / tick | no |
| `windows.*`, `slos.*` | `Maintenance.tsx:57,89`; `Slos.tsx:101,154,242` | direct | tick / id | no |
| `alerts.rules/channels/rule` | `Alerts.tsx:37-45, 96, 207` | direct | tick / id | no |
| `dashboards.list/get` | `Dashboards.tsx:30, 93`; `QueryBuilder.tsx:304` | direct | tick / id / mount | no |
| `ai.list/get` | `Ai.tsx:20, 197-207` (1.5 s poll while running) | direct | id / poll | poll only |
| `checks.screenshot` | `CheckDetail.tsx:408-414` | direct | run | yes |
| Mutations | handlers across Settings, Synthetics, CheckDetail, Maintenance, Slos, Alerts, Dashboards, QueryBuilder, DropRules, GetStarted, Ai | handler | n/a | n/a |

### 4.3 Calls outside `api.ts` or the hooks

- **Raw `fetch` outside `api.ts`:** only `config.ts:10` (`/config.json`), plus the dev-only `mock/mockApi.ts:781`.
- **Telemetry queries outside the shared hooks:**
  - Dashboards `usePanelData`, QueryBuilder `useResults`, Ai insights, GetStarted polling, DropRules previews (custom effects).
  - `Sql.tsx`, Alerts preview, DropRules Preview button (event handlers).
- **About 30 REST loaders** are direct promises in `useEffect`; most have no late-response guard (section 4.2).

---

## 5. Pages and the queries they issue

**Markers**
- **X-n:** the same or nearly the same query is issued on several pages.
- **S-n:** the same query is issued twice on one page.

| Page (route) | Queries on load | Duplicates |
|---|---|---|
| Home `#/` | 19 JSON + `checks.list`, all at once | X-1 service lists; X-2 `checks.list`; X-3 synthetic runs; X-4 check success; X-5 span heat map = Traces; X-6 error spans = Traces; X-7 span percentiles = Traces; X-8 log severity histogram = Logs; X-10 metric names = Metrics and Query Builder; S-1 service list twice |
| Logs `#/logs` | 3–7 JSON | X-8; X-1 (drop-down) |
| Traces `#/traces` | 4–5 JSON; waterfall 2; span panel 2 | X-5, X-6, X-7; X-1 |
| Metrics `#/metrics` | 3 list / 5 detail | X-10 |
| Service map `#/services` | 2 SQL; panel 1–2 | X-12 service group (≈ Lambda list); X-13 service series (≈ Lambda series) |
| Web `#/web` | 8 SQL | |
| Lambda `#/lambda` | 2 SQL list; 4–5 per function | X-12, X-13 |
| Kubernetes `#/kubernetes` | discovery + 5 on Overview (+ drawer 3) | X-14 discovery = GPUs; S-2 workloads; S-3 events |
| GPUs `#/gpus` | discovery + prices + 4–6 by tab (+ drawer 2) | X-14; X-16 GPU prices = Settings; S-4 `gpuHealth`; S-5 `gpuXids`; `gpuBuckets` CTE scanned 4× |
| Dashboards `#/dashboards` | list; view: get + 4 variable queries + one PromQL per panel query (≤6 at once) | X-1 (services ×3 signals) |
| Query Builder `#/query` | metric list + one PromQL per query (per keystroke) | X-10 |
| SQL `#/sql` | on Run only | |
| Alerts `#/alerts` | rules, channels, `checks.list`, `slos.list`; history; rule-form previews | X-2 |
| Synthetics `#/synthetics` | `checks.list` + 3 JSON | X-2, X-3, X-4 |
| Check detail | get + 8 JSON (+ run 1, screenshots) | X-3; S-6 `prev`/`tally` |
| SLOs `#/slos` | `checks.list`, `slos.list`, 2–4 per SLO | X-2; N+1 |
| Maintenance | `checks.list`, windows | X-2 |
| Leasyd AI `#/ai` | list, 7 PromQL, `checks.list` | X-2; PromQL ≈ built-in Service overview dashboard |
| Get started `#/start` | `account.get` (10 s poll), 3 JSON (20 s poll) | X-15 `account.get` |
| Drop rules, Settings, Usage | account, meters, rules; 2 JSON per drop rule; 3 JSON (24 h) | X-15, X-16, X-9 |

`account.get` (X-15) is also loaded by the trial banner on every page.

---

## 6. Global state

| State | Lives in | Persisted | Consumers | Notes |
|---|---|---|---|---|
| Time range | `App.tsx:44` `useState` | localStorage `leasyd.range` | every page via the `ctx` prop (no React context); Shell picker | Survives navigation and reload. **Not in the URL**, so a shared link opens with the reader's own range. "Now" is computed per component, effect or click (25 `rangeWindow` sites), so panels on one page can end a few ms apart. |
| Ignores the global range | | | Usage (24 h), SLOs (their window), trace waterfall (30 d), DurationCompare (±30 min), check run panel (±1 h), Lambda invocation logs (±5 min), Drop rule previews (24 h), Get started (15 min), Alerts preview (3 h), discovery (last 15 min of range) | Each is deliberate for its view; none says so in the UI except Usage |
| Refresh tick | `App.tsx:45` | memory | `ctx.tick` everywhere | Only the Refresh button (`Shell.tsx:101`) changes it. **No auto-refresh anywhere.** Page-local timers: GetStarted polls (10 s / 20 s), Ai conversation (1.5 s), CheckDetail 60 s after "Run now" (not cleared on unmount), Settings 3 s / 8 s after Stripe. About a dozen views ignore the tick (listed in 4.1 and 4.2). |
| Selected service | per page (`Logs.tsx:54`, `Traces.tsx:59`, `Metrics.tsx:149`, `Web.tsx:40`, `ServiceMap.tsx:76`, `Dashboards.tsx:88`) | URL read once at mount, never written | its own page | Duplicated per page; a link to the same page with a new `?service=` is ignored (the page isn't remounted) |
| Filters, tabs, views | component state per page | mostly memory | | Only the Query Builder writes its state to the URL (`QueryBuilder.tsx:108-111`, `replaceState`). K8s and GPUs read `?tab=`, `?pod=` and `?gpu=` at mount. |
| UI preferences | localStorage | `leasyd.cols.*`, `leasyd.fold.*`, `leasyd.dash.fav`, `leasyd.sql`, `leasyd.theme` | | Inline try/catch in five places instead of one helper |
| User / auth | `App.tsx:43`; Cognito library storage | | Shell | A 401 mid-session shows an error on the page; it doesn't return to sign-in |

---

## 7. Frontend module graph

**Layers (as found):**
```
main → App → Shell, pages/*, api, auth, config, time
pages/* → components/*, infra/*, useQuery, api, time, config, pricing, builtinDashboards, OTHER PAGES, ../App (type)
components/* → time            (Panel → useQuery: type Loaded)
infra/useInfra → api, infra/queries, infra/model;  infra/* → infra/model;  infra/format → time
useQuery → api;  api → auth, config, infra/model (type);  auth → config;  builtinDashboards → api (type)
```

**Cycles:**
- `pages/Synthetics` ↔ `pages/CheckDetail` is a **runtime cycle**:
  - `Synthetics.tsx:9` imports `CheckDetail`.
  - `CheckDetail.tsx:13` imports seven helpers and components from `./Synthetics`.
  - It works only because every use is inside a function.
- `App` ↔ every page is a type-only cycle (`import type { Ctx } from "../App"`). It is erased at build.
- There is no cycle between `api` and `auth`.

**Pages importing other pages** (shared helpers live in page files):
- Insights → Logs, Traces
- Traces → Logs
- Lambda → Traces (`DurationCompare`, `isError`, `KV`, `Span`)
- Alerts → Logs (`fmtTs`)
- CheckDetail → Logs, Synthetics
- Synthetics → CheckDetail, Maintenance, Logs

**Layer leaks:**
- `api.ts` imports a domain type from `infra/model`.
- `components/Panel.tsx` imports from the hook module.
- `time.ts` also holds number formatting.

**Where queries are built:**
- In modules: `infra/queries.ts`, `builtinDashboards.ts`, `infra/alertTemplates.ts`.
- Inline in pages:
  - SQL: ServiceMap, Web, Lambda.
  - PromQL: QueryBuilder, Ai, Dashboards.
  - JSON: Insights, Logs, Traces, Metrics, Synthetics, CheckDetail, Slos, Alerts, Usage, GetStarted, DropRules.
- The SQL quoting helper `q()` is copied four times: `infra/queries.ts:14`, `ServiceMap.tsx:35`, `Web.tsx:18`, `Lambda.tsx:25`.

---

## 8. Backend

### 8.1 Lambdas and handlers

| Function | Handler | Memory / timeout | Template |
|---|---|---|---|
| obs-query-api | `query.api` | 2048 MB / 30 s | `infra/phase4-query.yaml:118-143` |
| obs-query (direct invoke: alerts, AI, MCP) | `query.handler` | 2048 / 300 s | `:46-70` |
| obs-query-worker | `query.worker` | 3008 / 300 s, 10 GB /tmp, async retries 0, max event age 300 s | `:72-108` |
| obs-index-lookup | `lookup.handler` | 1024 / 30 s | `infra/phase3-index.yaml:17-29` |
| compaction, fast lane, sealer | `handler.*` | | `infra/phase2-compaction.yaml` |
| obs-ingest, obs-ingest-authorizer | `ingest.handler`, `authorizer.handler` | | `infra/phaseT2-ingest.yaml` |
| obs-account-api, obs-tenant-admin | `account.handler`, `admin.handler` | | `infra/phaseT5-tenants.yaml` |
| obs-alerts | `alerts.handler` (API, evaluate, on_result) | | `infra/phaseA1-alerts.yaml` |
| obs-synthetics-api, -tick, -run, -browser | `synthetics.*`, `browser.handler` | | `infra/phaseS1-synthetics.yaml` |
| obs-ai, obs-mcp | `sre.handler`, `mcp.handler` | | `infra/phaseAI-sre.yaml` |
| obs-canary | `canary.handler` | | `infra/phaseT7-monitoring.yaml` |

No function sets reserved concurrency.

### 8.2 The query endpoint (`services/compaction/query.py`)

**Steps of `query.api` (`query.py:768-816`):**
1. **Tenant:** taken from `authorizer.claims["custom:tenant"]` (Cognito; the attribute is immutable, `infra/state.yaml:212`) or from `authorizer.tenant` (API key), checked against `layout._TENANT`. Missing or invalid gives 401. A `tenant` in the body is dropped (`API_FIELDS`, `:825`).
2. `GET /v1/app/me` returns `{tenant, email}`.
3. Any other GET is a job status.
4. Rate limit, then decode the body.
5. With `"async": true` it starts a job immediately.
6. Otherwise `answer()` runs in a thread with a 20 s deadline.

**Kinds, dispatched in `answer()` (`:819-843`):**
- **PromQL** (`promql.py`): each selector becomes JSON engine queries.
- **SQL** (`run_sql`, `:497`):
  - `check_sql`: one SELECT only; no table functions; only the tables logs, spans and metrics, or CTEs.
  - One index lookup per table, then caps of 4,000 files and 1 GiB.
  - One `obs-query-worker` invocation, which downloads the files and runs DuckDB.
- **JSON engine:** `compile_query` (generated SQL with whitelisted columns and bound parameters) → `lookup.lookup` → `plan_chunks` → fan-out to up to 64 workers (one chunk runs in the API Lambda) → `merge`.

**Limits:**

| Limit | Value |
|---|---|
| Rows | JSON 10,000; internal PromQL 50,000; SQL 10,000 with a `truncated` flag |
| Response size | 5.5 MB, else 413 |
| Rate (per minute) | queries 300 (free) / 600 (standard); jobs 5 / 10 |
| Daily search units | 2,000 / 20,000. The check fails open on DynamoDB errors |
| Per-tenant concurrency | none |

### 8.3 Job lifecycle

| Step | What happens |
|---|---|
| Start | `start_job` (`query.py:850`) writes `{"status":"running"}` to `s3://<data bucket>/_results/jobs/<tenant>/<32-hex>.json`, invokes `obs-query-worker` with `Event`, and returns 202 `{job}` |
| After the 20 s deadline | The job **starts the query again**. The sync thread is abandoned (`pool.shutdown(wait=False)`, `:805-816`), not stopped, so the work runs twice until the API Lambda is frozen. Units used by the abandoned thread are charged late or never (`charge_units` runs in `finally`). |
| Worker | `run_job` (`:945-964`) stores `{"status":"done","answer":…}`, or a 500 answer on failure |
| Status | `GET …/query/{job}` (`:967-982`): the id must match `^[0-9a-f]{32}$`; the key is built from the **authorizer's** tenant, so another tenant's job id gives 404 (tested, `test_query.py:637`). Running longer than 330 s gives 504. A job can still finish after its 504 and overwrite the state with `done`. |
| Expiry | S3 lifecycle: `_results/` expires after 1 day (`infra/state.yaml:74-78`) |
| Cancel | **None.** The client stops polling only by giving up after 6 minutes. |

### 8.4 DuckDB

**Connections:** a new in-memory connection per request (`compact._connect`, `compact.py:212-220`), with:
- `memory_limit` at 60% of the Lambda's memory;
- `temp_directory` in `/tmp`;
- `TimeZone='UTC'`.

Nothing is reused across invocations. `import duckdb` is at module level, so every `obs-query-api` cold start loads DuckDB, including `/me` and job polls.

**Lock-down:**
- **SQL worker** (`query.py:566-570`), applied after the views are built: `allowed_paths` (local Parquet only), `enable_external_access=false`, no extension autoinstall or autoload, `lock_configuration=true`. There is no scalar-function allow or deny list; the lock-down settings are the defence. `check_sql` runs in both the API and the worker.
- **JSON engine:** none of these settings. Its SQL is generated with bound parameters.

**File access:**
- The JSON engine reads Parquet in place through the tenant-scoped fsspec `TenantS3` (S3 range GETs) and downloads raw JSON.
- The SQL worker downloads every file to `/tmp` with 64 threads.

### 8.5 Tenant isolation

- **Data reads** use a tenant-scoped role:
  - `lookup._clients_for(tenant)` assumes `obs-tenant-reader` with session tag `tenant` (`lookup.py:179-202`).
  - IAM then allows `s3:GetObject` only on `data/tenant=${tag}/*` and `_incoming/tenant=${tag}/*`, and DynamoDB keys `${tag}#*` (`infra/phase0-foundation.yaml:407-457`).
  - The query role itself has no data access.
- **Job results** (`_results/*`) and the rate-limit keys are scoped only by how the code builds the key, not by IAM.
- **Internal invocations** (`query.handler`, `query.worker`, `lookup.handler`) take the tenant from the payload. Only internal roles can invoke them, and IAM still scopes the data.

### 8.6 Auth per route (API Gateway `obs-ingest`, stage `ingest`)

| Routes | Auth |
|---|---|
| All `/v1/app/*` (query, query/{job}, me, account, ai, checks, windows, slos, alerts, dashboards) | Cognito user pool |
| `/v1/logs`, `/v1/traces`, `/v1/metrics` | API-key authorizer (ingest scope) |
| `/v1/query`, `/v1/query/{job}`, `/v1/mcp` | API-key authorizer (read scope) |
| `/v1/aws/cloudwatch-metrics` | Firehose access-key authorizer |
| `/v1/rum` | **None**; public browser key and origin allow-list checked in `rum.py` |
| `/v1/signup` | **None**; throttled 1 rps, per-address and per-day counters |
| `/v1/stripe/webhook` | **None**; HMAC Stripe signature |

Cognito routes have no usage plan, only the stage limit of 5,000 rps.

### 8.7 Error mapping

| Condition | Answer |
|---|---|
| Bad body, `BadQuery`, `ValueError`, DuckDB error | 400 |
| Over rate or daily units | 429 |
| Result too big | 413 |
| Workers throttled | 503 + `Retry-After: 30` |
| Unhandled error | Lambda error; API Gateway's DEFAULT_5XX becomes 503 + `Retry-After: 10` |
| Job running / done / stale / unknown | 202 / stored answer / 504 / 404 |

### 8.8 Tests

`services/compaction` has 232 tests, including:
- the engine against moto;
- tenant isolation;
- jobs, including the cross-tenant 404 and the 504;
- the SQL lock-down;
- the K8s/GPU contracts.

Other services have 3–34 tests each.

The frontend has one test file (`infra/analysis.test.ts`). **There are no tests of the hooks, `api.ts`, polling or pages.**

---

## 9. Infrastructure and hosting

**Stacks:** about 20, all named `obs-*`.
- **Stateful** (Retain): `obs-state` (data bucket, tables, Cognito).
- **Per phase:** `obs-phase0` (roles, boundary, budget) through `obs-phaseW1` (web) and `obs-phaseAI`.
- `infra/up.sh` and `down.sh` deploy or remove them in order, but `obs-phaseAI`, `obs-phaseE1` and `obs-site` are outside up/down. After a down/up the AI routes would keep the old API id.

**Web hosting** (`infra/phaseW1-web.yaml`, `infra/deploy-phaseW1.sh`):
- Private S3 bucket read by CloudFront through OAC.
- `/v1/*` goes to the API stage: same origin, no CORS, caching disabled.
- `/config.json`: caching disabled.

| Files | Cache-Control | CloudFront |
|---|---|---|
| `assets/*` (hashed) | `public,max-age=31536000,immutable` | |
| Everything else (`index.html`, `config.json`, `global.js`, `install/**`) | `no-cache` | Invalidation of `/`, `/index.html`, `/config.json` on deploy |

**Old asset files:** deleted on each deploy (`--delete`). The app is one bundle with no lazy chunks, so an open tab already has all its code.

**Versioning:**
- There is no API versioning beyond the `/v1` prefix, and no build-version check.
- A tab opened before a deploy keeps the old bundle against the new API until it is reloaded.

**Security headers:**
- Set on the default behaviour: CSP (`script-src 'self'`; `style-src` allows `'unsafe-inline'`), HSTS, frame-ancestors none, nosniff.
- Not set on `/config.json` or `/v1/*`.
- There is no WAF.

**Query path IAM:**
- `obs-query` may assume `obs-tenant-reader` (tenant-scoped) and invoke the worker.
- It has read/write on `_results/*` (not tenant-scoped).
- It has a **dangling grant on DynamoDB table `obs-cache`**, which no template or code defines (`infra/phase0-foundation.yaml:380-385`).

**Monitoring:** error alarms on obs-query, obs-query-api, obs-query-worker and obs-index-lookup; app-route and canary alarms; dashboard `Leasyd-health`. There is no alarm on async job failures, query latency, obs-mcp errors or API 4XX.
