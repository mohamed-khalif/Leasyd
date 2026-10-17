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
- [ ] **Design for review: docs/design-query-lifecycle.md** (no double run, per-tenant cap, cancel, fail-closed LKG, password-flow migration) -- WAITING FOR APPROVAL
- [ ] No double run (design §2)
- [ ] Per-tenant in-flight cap (§3)
- [ ] Fail closed with last-known-good (§5)
- [ ] Backend cancel (§4.2)
- [ ] ingest-throttled alarm (T7)

## Phase 5 (safety net), bug fixes, Phase 4 (caching, STOP), Phase 6, Phase 7: not started
