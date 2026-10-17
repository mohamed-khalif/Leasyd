# Phase 4: caching decision

Status: **recommendation, waiting for your choice.** Nothing here is built yet.

## What the measurements say (code-audit.md §1)

- Within one page load there are almost no exact duplicates. Home's 20 queries are 20 different queries.
- The waste is **across time and components**:
  - every navigation re-sends every query, even after a few seconds. Home → Traces → Home sends Home's 20 queries again;
  - K8s and GPUs each re-run the same discovery SQL on every visit;
  - `account.get` is loaded twice on Settings, Drop rules and Get started, plus once by the banner on every page;
  - `checks.list` had 8 loaders with different refresh rules (3 now share one hook after B3).
- Queries are expensive: p95 2.7 s on the API Lambda, and each one is several Lambda invocations.
- A cache cannot help when the key changes. Today every hook builds its window from `Date.now()`, so the key changes on each visit. **A shared "now" is the precondition for any cache hit.**

## The options

| | (a) Leave as is | (b) Small in-house layer | (c) TanStack Query |
|---|---|---|---|
| What | Nothing | One module next to `api.ts`: in-flight de-duplication, plus answers kept fresh for a short time (30 s), keyed by normalised query, window and refresh tick. Used by the one core hook (Phase 6 step 1). | `@tanstack/react-query` 5.104.4 behind the existing hook signatures, with one `QueryClient` |
| Bundle | 0 | About +2 kB gzip (estimate, about 150 lines) | **+9.9 kB gzip** (33 kB minified; measured), +4.5% on today's 221 kB |
| New package | No | No | **Yes.** You allowed only happy-dom, so this needs your yes |
| Effort | 0 | About 1–2 days, including tests. Most of it is the core hook, which Phase 6 step 1 builds anyway | About 1–2 days. Similar wrapping work, plus turning its defaults off |
| Behaviour risk | None | Low. We write exactly the rules below, and the Phase 5 tests pin the rest | **Medium.** Its defaults change behaviour unless turned off: 3 retries (a failing 3 s query costs 4×), refetch on window focus and reconnect (that is auto-refresh, which you ruled out), and keeping old data under a new key. All can be turned off, but each is a place to get it wrong. |
| Fits our flows | n/a | The 202 → job → poll → DELETE-cancel flow is ours. The layer hands an `AbortSignal` down and calls our cancel. | It passes an `AbortSignal` too, but job cancel and poll stay our own code. Its retries must never re-POST a running job. |
| What you get beyond (b) | – | – | Devtools; garbage collection; structural sharing; mutation helpers. Useful if the app grows many mutations or pagination. Not needed now. |

## Recommendation: (b), the small in-house layer

**Why:**
- It gets the savings the measurements point to, adds no package, and adds almost no bundle.
- Its behaviour is exactly what we write and test.
- (c)'s extra features don't address anything measured, and its defaults work against two of your rules: no auto-refresh, and no hidden retries of expensive queries.
- If the app later needs mutations or pagination at scale, moving from (b) to (c) is a swap behind the same core hook.

## How it would work (b)

**One shared "now" per page.**
- The range provider (Phase 6 step 3) holds `now`. It sets a new `now` when:
  - the range changes;
  - you press Refresh;
  - you open a page when the current `now` is older than the freshness time (30 s).
- Every panel on a page uses that one `now`. Home goes from several "now"s to one.
- Moving between pages within 30 s keeps the same window, so the cache can hit.
- This is not auto-refresh: nothing changes while you stay on a page.

**Keys.** A key is made of:
- the normalised query (JSON with sorted keys, or the SQL/PromQL text);
- the window (start, end);
- the refresh tick.

**Manual Refresh bypasses the cache.**
- It bumps the tick and sets a new `now`, so every key is new and everything is asked again. That is the same request count as today.
- It never shows an answer older than the press.

**Freshness: 30 s.**
- Within 30 s, the same key is answered from memory with no request.
- After 30 s, the entry is dropped (not served stale), so the page shows "refreshing" over the old data, as approved for K8s/GPU.

**In-flight de-duplication.**
- Two panels asking the same key at the same time share one request.
- If one of them unmounts, the request is aborted only when no one else is waiting for it.

**Errors are never cached.** The next ask tries again. B3 found Dashboards caching a failure for the whole session.

**Writes invalidate.**
- Saving a check, an SLO, a drop rule, GPU prices or the account clears the matching REST entries (`checks.list`, `account.get`, …).
- Query results are not affected by writes.

**Memory.**
- At most 200 entries, least recently used dropped first.
- Large results (over 2 MB) are not kept.

## The other items you asked for, under (b)

| Item | Plan | Saves (from the measurements) |
|---|---|---|
| Shared "now" | Above. The provider is Phase 6 step 3, and `useNow` / `useRangeWindow` (Phase 5) become reads of it. | The precondition for every hit below. It also makes all of Home's panels agree on one window. |
| Shared K8s/GPU discovery | Discovery SQL goes through the cache under its own key (window and tick, no filters). K8s and GPUs share it. | K8s → GPUs → K8s → GPUs: 4 discovery runs become 1 within 30 s. Revisits within 30 s send 0 page queries instead of 5–6. |
| Check-list loaders (8) | One `useCheckList` (exists since B3, used by 3 pages) on the cache. Insights, Synthetics, Alerts, Dashboards and AI move to it. Writes to checks invalidate it. | 1 request per 30 s instead of 1 per component. Dashboards' session-long cache goes away. |
| `account.get` (C12) | Same: one cached loader shared by the banner and pages. | 1 request instead of 2 on 3 pages, and the banner's per-navigation request within 30 s. |
| SQL quoting helpers (4 × `q()`) | One `lib/sql.ts` with `sqlString()` and `sqlIdent()`, with tests. The four copies are deleted. This doesn't depend on the cache choice (Phase 6 step 5). | Consistency, no request change |
| Home's 20 queries on first load | Not a cache problem: there are no duplicates. This is P7 (combine overlapping card queries) in Phase 6 step 7. | Expected about 20 → 14 |

**Expected effect, Home → Traces → Home within 30 s:** about 24 requests become about 4.
- Traces sends its 3.
- Home sends 0 queries; `me` and the banner are cached.
- Refresh is unchanged: everything is asked again.

## What I need from you

1. Choose (a), (b) or (c). If (c): allow `@tanstack/react-query` as a dependency.
2. Confirm the freshness time of **30 s**, or pick another.
