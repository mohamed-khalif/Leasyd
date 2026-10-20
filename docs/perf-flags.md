# Performance flags

Each flag is an environment variable on the function named, listing the accounts it is on for
(comma-separated; `*` = every account; empty or unset = off). Plan: `docs/perf-plan.md`.

| Flag | Function | Phase | What it turns on | Default |
|---|---|---|---|---|
| (none yet) | | | | |

Always on (no behaviour change): the per-question timing log line `{"question": …}` in `obs-query`
and the `stats.lambda` timing workers return (Phase 0).

## Running costs

| Item | Phase | Estimate | Measured |
|---|---|---|---|
| Provisioned concurrency 3 + 3, business hours | 3 | about $45 a month | |
| Result cache (S3, DynamoDB) | 2 | a few dollars a month | |
