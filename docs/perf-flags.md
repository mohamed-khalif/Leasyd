# Performance flags

Each flag is an environment variable on the function named, listing the accounts it is on for
(comma-separated; `*` = every account; empty or unset = off). Plan: `docs/perf-plan.md`.

| Flag | Function | Phase | What it turns on | Default |
|---|---|---|---|---|
| `SQL_IN_PLACE_ACCOUNTS` (stack parameter `SqlInPlaceAccounts`) | `obs-query` | 1 | A SQL question run by one worker reads Parquet in place (only the columns and row groups it needs) instead of downloading whole files | empty |
| `SQL_SCAN_FANOUT_ACCOUNTS` (stack parameter `SqlScanFanoutAccounts`) | `obs-query` | 1 | The scan fan-out (`services/compaction/sqlscan.py`): many workers read, keeping only the columns and rows the question can use; the question runs unchanged over that. Falls back to the single worker when a slice's rows exceed 4 MB, a worker fails, or the query fails over the kept rows | empty |
| `RESULT_CACHE_ACCOUNTS` (stack parameter `ResultCacheAccounts`) | `obs-query` | 2 | Answers kept for repeat questions: in memory and in S3 (`_results/cache/`), time range rounded down to 30 s; kept 30 s when the range reaches into the last 15 min, else 10 min | empty |
| `HOT_ACCOUNTS` (stack parameter `HotAccounts`) | `obs-query` and `obs-ingest` | H1 | Ingest copies the account's batches to the `obs-hot` stream; `obs-query` asks the hot store about the last 2 hours and the files about the rest (`docs/hot-store.md`). Falls back to the files whenever the store is down, behind or doesn't cover the range | empty |

Warming (always on unless `WarmSchedule=""`): rule `obs-query-warm` every 5 minutes, weekdays 06:00-18:59 UTC, holds
3 `obs-query`, 128 `obs-query-worker` and 3 `obs-query-api` containers busy for 0.5 s so they stay ready.

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
| Warming pings (3 + 128 + 3 containers, 0.5 s, every 5 min, business hours) | 3 | about $5-10 a month | |
| Result cache (S3, DynamoDB) | 2 | a few dollars a month | |
| Hot store: Kinesis on-demand, Fargate 1 vCPU / 4 GB, 2 interface endpoints x 2 zones | H1 | about $95-130 a month | |
