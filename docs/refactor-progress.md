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

## Phase 4 (decide caching), 5 (safety net), 6 (refactor), 7 (verify): not started
