# `src/`: the web app's code

How data flows through the app is in `docs/ARCHITECTURE.md` §0. This file is a map: where to find things, and where new code goes.

## Folders

| Folder | Holds | Notes |
|---|---|---|
| `pages/` | One file per page, plus page folders for code shared between pages: `pages/synthetics/` and `pages/settings/` | See `pages/README.md`. |
| `components/` | Shared UI: `Panel`, `Loads`, `TimeSeries`, the charts, `RankTable`, `Stat`, `Capped` (500 rows at a time), `ErrorBoundary`, `SpanDetails` | Components have no queries of their own. |
| `infra/` | Kubernetes and GPU pages: `queries.ts` (every SQL query, as plain functions), `useInfra.ts` (their hooks), `model.ts`, `analysis.ts`, `alertTemplates.ts` | `queries.ts` is tested against the real engine (`services/compaction/test_infra_queries.py`). |
| `lib/` | Plain functions, no React | See `lib/README.md`. |
| `test/` | Test helpers: `dom.ts` (happy-dom, `render`, `settle`), `mockBackend.ts` (`fetch` answered by `mock/mockApi.ts`), `fetchStub.ts` | |

## Top-level modules

**App shell**

| Module | Role |
|---|---|
| `main.tsx` | Start-up: load the config, then render `App`. |
| `App.tsx` | Sign-in gate, hash router, range, Refresh tick and the page's "now" (`Ctx`), per-page code loading (`splitPage`, `preloadPage`). |
| `Shell.tsx` | Sidebar, top bar (range picker, Refresh, theme), trial banner. |

**Server access and auth**

| Module | Role |
|---|---|
| `api.ts` | Every request to the server. Typed API objects, the query/job flow and `ApiError`. |
| `auth.ts` | Cognito sign-in, new password, forgot/reset, change password, ID token. |
| `config.ts` | Loads `/config.json`. |

**Data hooks**

| Module | Role |
|---|---|
| `useQuery.ts` | `useLoad`, the one core data hook, and its wrappers `useQuery` and `useSql`. |
| `useAccount.ts` | The one account loader. |
| `checkList.tsx` | The one check-list loader. |
| `limiter.ts` | At most 6 queries in flight per page. |

**Time range, links and typing**

| Module | Role |
|---|---|
| `useNow.ts` | `useRangeWindow(ctx)`, the page's time window; `useNow(...)` for views with their own window. |
| `time.ts` | Ranges, custom ranges, `rangeWindow`, buckets, retention. |
| `hashParams.ts` | Reading and writing `?param=` in the link (`useHashParam`, `useHashSearch`). |
| `useDebounced.ts` | Typing waits 400 ms; Enter or Run goes at once. |

**Static content and helpers**

| Module | Role |
|---|---|
| `builtinDashboards.ts` | The read-only built-in dashboards. |
| `pricing.ts` | List prices. Keep them in step with `site/pricing.html` and `site/pricing.js`. |
| `csv.ts` | CSV export, with formula injection blocked. |
| `devRequests.ts` | Dev only: a log of every request, for measuring. |
| `icons.tsx` | SVG icons. |
| `styles.css` | All CSS. |

## Rules (lint enforces the first four)

1. **Requests go through `api.ts`.** `fetch` is allowed only there and in `config.ts`.
2. **Pages don't import pages.** Shared code goes in `lib/`, `components/`, `infra/` or a page folder.
3. **Formatting is done only in `lib/format.ts`.** `toFixed`, `toPrecision`, `toLocale*` and `Intl` are not allowed in `pages/` or `components/` (`docs/formatting.md`).
4. **React hooks rules apply.** An effect keyed by hand says why, in its `eslint-disable` comment.
5. **A query's window comes from `useRangeWindow(ctx)` or `ctx.now`.** Never read `Date.now()` per panel: one "now" per page.
6. **Fetch data with `useLoad`** (or a wrapper), so a new key or leaving the page aborts the request.
7. **A failed load is shown as an error,** never as empty, zero or "all clear". `failures.test.tsx` checks this.
