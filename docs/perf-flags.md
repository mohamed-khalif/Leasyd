# Performance flags

Each flag is an environment variable on the function named, listing the accounts it is on for
(comma-separated; `*` = every account; empty or unset = off). Plan: `docs/perf-plan.md`.

| Flag | Function | Phase | What it turns on | Default |
|---|---|---|---|---|
| `SQL_IN_PLACE_ACCOUNTS` (stack parameter `SqlInPlaceAccounts`) | `obs-query` | 1 | A SQL question run by one worker reads Parquet in place (only the columns and row groups it needs) instead of downloading whole files | empty |
| `SQL_SCAN_FANOUT_ACCOUNTS` (stack parameter `SqlScanFanoutAccounts`) | `obs-query` | 1 | The scan fan-out (`services/compaction/sqlscan.py`): many workers read, keeping only the columns and rows the question can use; the question runs unchanged over that. Falls back to the single worker when a slice's rows exceed 4 MB, a worker fails, or the query fails over the kept rows | empty |

Set them with `infra/deploy-phase4.sh --parameter-overrides SqlScanFanoutAccounts=leasyd-demo SqlInPlaceAccounts=leasyd-demo`
(other parameters keep their values). The SQL page and the app's pages both use these: their requests have the
same shape, so one flag covers both (the design's separate SQL-page flag would have needed an API change).

Direct invocations of `obs-query` can force a path with `"sql_path": "single" | "in_place" | "fanout"` (the
correctness gate, `infra/bench/sql_gate.py`); the API never passes it.

Always on (no behaviour change): the per-question timing log line `{"question": …}` in `obs-query`
and the `stats.lambda` timing workers return (Phase 0).

## Running costs

| Item | Phase | Estimate | Measured |
|---|---|---|---|
| Provisioned concurrency 3 + 3, business hours | 3 | about $45 a month | |
| Result cache (S3, DynamoDB) | 2 | a few dollars a month | |
