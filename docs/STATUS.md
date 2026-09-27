# Status

Where the project stands, what is running on AWS, and how to pick it up.
Last updated: 2026-09-27. Detail: `docs/observability-plan.md` (the plan, with results per phase)
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
| Fast lane: data searchable in ~35 s | T3 | live, tested |
| Traces and metrics | T4 | live, tested |
| Tenant operations: create, rotate, revoke, usage, delete | T5 | live, tested |
| Scale fixes: blooms out of the index, parallel lookups, day filters | T6 | live, tested |
| Load tests 1 -> 10 -> 50 GB/h, faults, soak | T6 | **waiting**: see below |
| Query engine, fan-out, UI, alerting | 4+ | not started |

Code: `services/` (compaction, ingest, tenants, loadgen), each with `pytest` tests.
Infra: `infra/*.yaml` (one CloudFormation stack per phase), `infra/deploy-*.sh`, `infra/*-test.sh`.

## Stacks on AWS

`obs-phase0`, `obs-phase1`, `obs-phase2`, `obs-phase3`, `obs-phaseT2`, `obs-phaseT5`, `obs-phaseT6`
(test tooling only; delete after T6). Deploys run as the `obs-deployer` IAM user; Phase 0 needs
admin credentials only when the `obs-boundary` policy changes.

## Blocked on

**Lambda concurrency limit is 10** (new-account default). Every function shares it, so the load
steps can't run and production traffic would be throttled. An increase to 1000 was requested in
Service Quotas (Lambda > Concurrent executions, us-east-1). Check with
`python3 infra/t6/loadtest.py preflight`.

## To resume T6 once the limit is raised

```bash
python3 infra/t6/loadtest.py preflight
python3 infra/t6/loadtest.py tenants create --n 100      # keys go to ~/.obs-t6-keys.json (not in git)
python3 infra/t6/loadtest.py run --gbph 1 --minutes 45 --step s1 --yes
python3 infra/t6/loadtest.py report --step s1
python3 infra/t6/loadtest.py compaction --since-minutes 180
# fix the first bottleneck found, then --gbph 10 (step s2) and --gbph 50 (step s3)
python3 infra/t6/loadtest.py tenants delete
```

Then: fault injection under load, a soak run, and delete `obs-phaseT6`.

## Loose ends

- Test tenants `t6-000`..`t6-004` exist (from the smoke runs). Their keys were kept only in the
  session's `~/.obs-t6-keys.json`; if that file is gone, delete them with
  `infra/tenant.sh delete t6-000` (etc.) and recreate with `tenants create`.
- Older test tenants from T2/T3 (`t2a-*`, `t2b-*`, `t2tiny-*`, `probe-*`, `t3-*`) can be removed
  with `infra/tenant.sh delete <tenant>`.
- Lock down Phase 0 after the lifecycle check (~2026-09-28): redeploy with
  `AllowTestAssume=false EnableLifecycleTest=false`.
- Merge the pull request from branch `claude/code-identification-1dxuoa` into `main`.

## Findings worth remembering

- New API keys take ~1 minute to work everywhere (they flicker 200/403 meanwhile); revocation ~1 minute.
- API Gateway may decompress gzip bodies but keep `Content-Encoding: gzip`; ingest checks the gzip magic bytes.
- Firehose occasionally delivers a file ~30 s later than usual (metrics once took 64 s to be searchable).
- At trickle volume, cost is ~$0.22/GB (per-request and per-file charges dominate); measure at volume.
