# Code health and data-consistency refactor: progress

Resume point for the review requested 2026-10-17. Rules: behaviour preserved by default; ask before
any behaviour change; one concern per commit; lint, type-check and tests green after every step.

## Phase 1: Map the system (read-only)
- [x] Frontend, backend and infra mapped (three read-only agents in parallel, merged and spot-checked)
- [x] `docs/ARCHITECTURE.md` written
- [x] Context claims checked (corrections listed in ARCHITECTURE.md, "Claims checked")

## Phase 2: Data consistency and freshness audit
- [x] Hook overlap
- [x] Key consistency and "now" per panel
- [x] Refresh behaviour
- [x] Time range and filters across pages / URL
- [x] Request lifecycle (abort, polling, backend cancel)
- [x] Auth during long operations
- [x] Error handling consistency
- [x] Duplicate load measurements (dev-only counter; StrictMode noted)
- [x] Cross-page connectivity
- [x] Deployment freshness (cache headers, old bundle vs new API)

## Phase 3: Code quality, performance and security audit
- [x] `docs/code-audit.md`

STOP after Phase 3 for review. **Phases 1-3 done 2026-10-17; waiting for review** (decisions needed: which bugs in
code-audit.md §3 to fix, behaviour changes C3/C4/C5, caching option in Phase 4).

Notes for resuming: the dev request log is `services/web/src/devRequests.ts` (window.__leasydRequests);
the measurement script was a throwaway Playwright run over `npm run mock` + `mock/engine_server.py`.

## Decisions (2026-10-17)
Fix all 15 bugs in code-audit.md §3 (one commit each, failing test first; order: B12 CSV, B10 MCP/AI units,
B3 failures as all-clear, error boundary (B4/B5), B1 QB stuck-loading, then the rest). Approved behaviour
changes: range + filters in the URL, every view responds to Refresh, "refreshing" instead of blanking K8s/GPU.
No auto-refresh. happy-dom (+ registrator) approved as dev deps; nothing else. Keep the password flow for now
(migration plan in design-query-lifecycle.md §6); optional TOTP MFA approved as a separate change.
Order: security -> Phase 5 safety net (incl. ESLint, clear/justify the 25 eslint-disable) -> bug fixes ->
Phase 4 recommendation (STOP for choice) -> Phase 6.

## Security (first)
- [x] SQL checker: over-nested query -> 400 (a9ea195)
- [x] SQL checker: CTE names scoped (f2aad5c)
- [x] Query timeout: DuckDB interrupt at 240 s (4dbd952)
- [x] Reserved concurrency: ingest 1000 / authorizer 800 reserved; worker 1000, api 300, coordinator 100 capped (0ea2294) -- not deployed; checked against ingest (design §1)
- [x] Reserved concurrency deployed 2026-10-17 and checked: all five set, ingest 0 throttles
- [x] Design docs/design-query-lifecycle.md approved 2026-10-17
- [x] No double run (§2) (494dc5a)
- [x] Per-tenant in-flight cap at 25 (§3) (4a4ae04) -- tighten to 4/12 after the client limiter (Phase 6)
- [x] Fail closed with last-known-good + obs-limits-refused alarm (§5) (ec1ce78)
- [x] Backend cancel (§4.2) (bbe2055) -- client side (§4.1) lands in Phase 6 step 2
- [x] obs-ingest-throttled alarm (T7)
- [x] Deployed and verified on AWS 2026-10-17 (canary tenant, through obs-query-api):
      fast query 200 inline (2.0 s); slow SQL -> 202 at 21.3 s, same run finished as a job (200 at 141 s;
      1 coordinator + 1 worker invocation, 133 s); endless SQL stopped at 240 s with a clear 400; cancel ->
      202, poll 410 at once, worker ended within 5 s, other tenant 404; 0 throttles on ingest and query
      functions; all query alarms OK.
- [x] Found on AWS: HeadObject of a missing marker is 403 without ListBucket -> every job poll failed (75ade31, deployed)
- [x] Found on AWS: a failed cancel check ended the timeout watch (commit after 75ade31) -- deploy phase4
- [x] obs-query cap needed --parameter-overrides CoordinatorConcurrency=400 (deploy keeps previous parameter values)
- [ ] Later, separate changes: password-flow migration (§6), optional TOTP MFA

## Phase 5 (safety net)
- [x] ESLint (flat config, typescript-eslint + react-hooks), `npm run lint` clean (37bb67a)
- [x] eslint-disable: 25 -> 20, each with a `-- reason`; shared "now" per page via useNow/useRangeWindow (37bb67a)
- [x] Test runner: node:test + esbuild, happy-dom (dev only) (4233908)
- [x] api.ts tests: auth, error mapping, 202 polling, give-up, transient 503 (4233908)
- [x] Hook tests: useQuery / useSql / useRows / useHint, current quirks pinned (21a6de4)
- [x] Page smoke tests: 22 routes against the mock backend
- [x] 45 tests pass; tsc and lint clean

## Bug fixes (order B12, B10, B3, B4/B5, B1, then the rest): done
- [x] B12 CSV export: formula-like text cells made inert, object URL revoked (fd64373)
- [x] B10 AI SRE + MCP queries use the daily allowance; alert queries' units no longer leak into the next API charge (da6a87f) -- deploy phase4 + phaseAI
- [x] B3 failures shown as errors (AI insights, who-am-I, GPU prices, drop-rules role, check lists, dashboard check names); shared useCheckList for 3 of the check-list loaders
- [x] B4 big results: chart/legend/waterfall bounds by loops, not spreads (e894cc8)
- [x] B5 bad links: safe decode, Query Builder link checked, decimals clamped; error boundary per page (f43e3a4)
- [x] B1 Query Builder stuck on loading (592af5b)
- [x] B2 over-nested SQL -> 400 (a9ea195, security step)
- [x] B6 dashboard variables: whole names, = with one value as written, user regex left alone (1a03d26)
- [x] B7 negative search.limit and RE2-invalid regex -> 400 (18088dd) -- deploy phase4
- [x] B8 transient poll failures retried, 3 in a row end the wait (78abcf9)
- [x] B9 charged once past the deadline: fixed by 494dc5a, now pinned by a test (39c4be8)
- [x] B11 Lambda request-id search: contains(), not ILIKE (80e97de)
- [x] B13 truncated results say so (Loads, K8s/GPU rows, service map, Lambda list) (11d1853)
- [x] B14 refused invokes free their slot; obs-query async events never retried, dropped after 60 s; late jobs not run; no overwrite after 504 (44c809c) -- deploy phase4
- [x] B15 one SQL run at a time; check re-read timer cleared (other two items done in B5/B13)
- [x] All 15 done. Deploy: phase4 (B7, B10, B14 + template), phaseAI (B10), web.
## Phase 4 (caching)
- [x] Recommendation written: docs/caching-decision.md (recommends (b), in-house layer, 30 s freshness)
- [x] Decided: (a) no cache. Keep: shared "now" per page, one check-list / account loader each (no reuse), one SQL quoting helper. Dropped: cross-page discovery sharing, in-flight de-dup
## Phase 6
- [x] 6.1 one core data hook (useLoad); useQuery/useSql/useHint/useRows wrap it (555d9a1)
- [x] 6.2 abort on new key/unmount; DELETE for abandoned jobs; limiter frees slots; QB aborts only on unmount (4e69cbb)
- [x] 6.3 one shared "now" per page (31a72d4); range in the link, C4 (8620692); Logs/Traces/Metrics filters in the link, C5 (b99feaa); bars skip NaN times while a new range loads (8cb4902)
- [x] 6.4 one check-list loader (cabcb8b) and one account loader (0964a44), no cache; Dashboards' session-long names cache removed
- [x] 6.5 one SQL quoting helper, lib/sql.ts (cfabec5); escaping fixed earlier in B6/B11
- [x] 6.6 page helpers out of pages, import cycle gone (eec25b1, 7880154); lint: fetch only in api.ts, pages don't import pages (3f7c203); dead code deleted (9866638)
- [x] 6.7 per-page code loading, first load 224 -> 109 kB gzip (a22d4ea), old assets kept on deploy, C15 (b182725); line chart (189574a) and heatmap (e40ab19) not redrawn on hover; ctx memoised (6ae663c); Logs patterns and QB series memoised (83c7076)
- [x] Deployed and checked on AWS 2026-10-17 (canary tenant): fast query 200 in 1.0 s; negative search.limit -> 400; RE2-invalid regex -> 400 (B7); async job 202 -> 200; slow job cancelled -> 202, poll 410; alert-style query charged 0 units, billed (AI/MCP) query 1 unit (B10); obs-query async config 0 retries / 60 s (B14); 0 errors, 0 throttles. Web app not checked from here (no network route to app.leasyd.com).
- [x] Typing waits 400 ms, Enter/Run at once (4f09a7e); lists draw 500 at a time with Show more, waterfall search + expand/collapse all over the whole trace, CSV has every row (3b14c61); client limiter, 6 queries in flight per page
- [ ] REVISIT INFLIGHT_LIMITS (server, per account; still 25 for every plan, by decision 2026-10-17) after a few days of real data with the client limiter deployed. The data: obs-query-api logs one {"inflight": n, ...} line per query started. Logs Insights on /aws/lambda/obs-query-api:
      filter ispresent(inflight) | stats max(inflight) as peak, pct(inflight, 99) as p99 by tenant, bin(1h)
      Earlier plan was 4 (free) / 12 (paid); pick from the observed p99 and peaks (one tab should stay at <= 6; several tabs or API clients add up).
- [ ] Unrelated, open: obs-fastlane-failed in ALARM since 2026-10-16 23:44 UTC (2 messages in the fast-lane DLQ)
- [x] Decided 2026-10-17 and done: debounce 400 ms with Enter/Run at once (4f09a7e); 500-row cap with Show more, waterfall search and expand/collapse all over the whole trace, CSV full (3b14c61); client limiter 6 per page (6497668) + in-flight log line (d81b8df), server limit left at 25; one formatter module lib/format.ts (798dd08, 31371ea), spec docs/formatting.md, lint rule (this commit's parent)
- [x] Deployed and checked 2026-10-17: obs-query-api logs {"inflight": 1, "tenant": "canary", "plan": "standard", "limit": 25} per query started
- [ ] Still open (no behaviour change, large diffs): P7 combine Home's overlapping queries (~20 -> ~14); F4 split oversized modules; F5 typed rows; treemap cap 200 tiles (not asked)
## Settings redesign (asked 2026-10-18, after Phase 6)
- [x] API keys can have a name ("prod ECS"): plain text up to 60 characters, stored on the key, shown in the list (8b466c5) -- deploy T5 (tenants)
- [x] Settings split into a menu and one page per topic at #/settings/<page>: Account (Profile, Security) and Organization (Organization, Team, Usage, Billing, API keys, GPU prices, AI tools). Each section: title and explanation left, controls right
- [x] Security: change password (Cognito ChangePassword, through the library already used; no new package)
- [x] Billing: plan notice (trial days left / pay as you go / ended / overdue), Stripe buttons, this month so far (estimate from the meter), prices, Enterprise "Book a demo". Stripe now returns to #/settings/billing; the old #/settings?billing=... still opens Billing
- [x] Behaviour changes, agreed in the plan: #/settings opens Profile (was one long page); the MCP command fills in a new read key on the API keys page (was on the same long page)
## Phase 7: not started
