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

## Phase 5 (safety net): next
## Bug fixes, Phase 4 (caching, STOP), Phase 6, Phase 7: not started
