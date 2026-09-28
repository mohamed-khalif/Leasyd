# Status

Where the project stands, what is running on AWS, and how to pick it up.
Last updated: 2026-09-28. Detail: `docs/observability-plan.md` (the plan, with results per phase)
and `infra/README.md` (deploy and test commands per phase).

## What exists

A serverless, multi-tenant observability back end on AWS (us-east-1, account 199301651524):

| Area | Phase | State |
|---|---|---|
| Storage, IAM boundary, budget ($50, alerts to AWSkhalif@gmail.com) | 0 | live |
| Compaction: raw OTLP JSON -> sorted Parquet, crash-safe | 2 | live, tested |
| Index lookups with bloom filters | 3 | live, tested |
| Tenant-aware storage and IAM isolation | T1 | live, tested |
| Authenticated ingest: API Gateway + API keys + per-tenant Firehose | T2 | live, tested |
| Fast lane: data searchable in ~30 s | T3 | live, tested |
| Traces and metrics | T4 | live, tested |
| Tenant operations: create, rotate, revoke, usage, delete | T5 | live, tested |
| Scale: day + hour ID filters, faster fast lane, fixes found under load | T6 | live, tested at 1, 10, 50 GB/h |
| Query engine with fan-out (`obs-query`), column-only reads | 4-5 | live, tested |
| Firehose cost cut (compress before Firehose) | T6 | live, tested: $0.143 -> $0.077/GB at 10 GB/h |
| Fast lane writes Parquet (queries read only Parquet) | T6 | live; queries pass at 50 GB/h, freshness p99 74 s (open) |
| Faults, soak | T6 | not started |
| UI, alerting on customer data | 6+ | not started |

Code: `services/` (compaction incl. query engine, ingest, tenants, loadgen), each with `pytest` tests.
Infra: `infra/*.yaml` (one CloudFormation stack per phase), `infra/deploy-*.sh`, `infra/*-test.*`.

## Stacks on AWS

`obs-phase0`, `obs-phase1`, `obs-phase2`, `obs-phase3`, `obs-phase4`, `obs-phaseT2`, `obs-phaseT5`,
`obs-phaseT6` (test tooling only; delete after T6). Deploys run as the `obs-deployer` IAM user; Phase 0
needs admin credentials only when the `obs-boundary` policy changes. Lambda concurrency limit: 1000.

## Key results (details in the plan)

- 50 GB/h across 100 tenants: every request accepted, freshness p99 44 s (target 60 s),
  ingest + fast lane $0.117/GB then; after compressing before Firehose, $0.077/GB at 10 GB/h.
- Query, largest tenant (1.26 GB/day): 1 day in 1.3 s with 19 workers reading only the needed
  columns (target < 5 s); trace ID across 30 days in 1.0 s (target < 3 s); results identical to Athena.
- Compaction 1.7x faster (profiled: JSON parsing, not the ID loops), identical output.

## Next

1. Freshness at 50 GB/h is p99 74 s (target 60 s) since compaction switched to the spill-safe
   parser (7b2f5d7), which the fast lane shares. Fix specified in `docs/NEXT-SESSION.md`;
   full state and benchmarks in `docs/REPORT-2026-09-28.md`.
2. T6 fault tests and a soak run; then delete the 100 `t6-*` tenants
   (`python3 infra/t6/loadtest.py tenants delete`) and the `obs-phaseT6` stack.

## Loose ends

- The `t6-*` tenant keys are only in the working session's `~/.obs-t6-keys.json`. If that file is
  gone, delete the tenants with `infra/tenant.sh delete t6-000` .. `t6-099`.
- Older test tenants (`t2a-*`, `t2b-*`, `t2tiny-*`, `probe-*`, `t3-*`, `t5-*`) can be removed with
  `infra/tenant.sh delete <tenant>`.
- Lock down Phase 0 (lifecycle check was due ~2026-09-28): redeploy with
  `AllowTestAssume=false EnableLifecycleTest=false`.
- Merge the pull request from branch `claude/code-identification-1dxuoa` into `main`.

## Findings worth remembering

- New AWS accounts start with a Lambda concurrency limit of 10; raise it before any real traffic.
- New API keys take ~1 minute to work everywhere (they flicker 200/403 meanwhile); revocation ~1 minute.
- API Gateway may decompress gzip bodies but keep `Content-Encoding: gzip`; ingest checks the gzip magic bytes.
- Load testing found and fixed: colliding bloom file names, a race deleting fast-lane blooms mid-lookup,
  compaction failing whenever DuckDB spilled to disk, and slow fast-lane indexing of big files.
