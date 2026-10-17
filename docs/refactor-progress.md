# Code health and data-consistency refactor: progress

Resume point for the review requested 2026-10-17. Rules: behaviour preserved by default; ask before
any behaviour change; one concern per commit; lint, type-check and tests green after every step.

## Phase 1: Map the system (read-only)
- [x] Frontend, backend and infra mapped (three read-only agents in parallel, merged and spot-checked)
- [x] `docs/ARCHITECTURE.md` written
- [x] Context claims checked (corrections listed in ARCHITECTURE.md, "Claims checked")

## Phase 2: Data consistency and freshness audit
- [ ] Hook overlap
- [ ] Key consistency and "now" per panel
- [ ] Refresh behaviour
- [ ] Time range and filters across pages / URL
- [ ] Request lifecycle (abort, polling, backend cancel)
- [ ] Auth during long operations
- [ ] Error handling consistency
- [ ] Duplicate load measurements (dev-only counter; StrictMode noted)
- [ ] Cross-page connectivity
- [ ] Deployment freshness (cache headers, old bundle vs new API)

## Phase 3: Code quality, performance and security audit
- [ ] `docs/code-audit.md`

STOP after Phase 3 for review.

## Phase 4 (decide caching), 5 (safety net), 6 (refactor), 7 (verify): not started
