# Status

Where the project stands, what is running on AWS, and how to pick it up.
Last updated: 2026-09-29. Detail: `docs/observability-plan.md` (the plan, with results per phase)
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
| Fast lane writes Parquet; per-tenant Firehose buffer (`tune`) | T6 | live, tested at 50 GB/h: freshness p99 38 s, queries pass |
| Failure visibility: fast-lane dead-letter queue + redrive, compaction/ingest/query alarms, freshness canary | T7 | live, each alarm proven by an injected failure |
| Customer query API: `POST /v1/query` with read-scoped keys (`infra/tenant.sh read-key`) | Q1 | live, tested on AWS (`infra/query-api-test.py`) |
| Customer logins: Cognito users per tenant (invite/remove), `/v1/app/me`, `/v1/app/query` | U1 | live, tested on AWS (`infra/login-test.py`) |
| Faults, soak | T6 | crash safety covered by tests and a real stuck-chunk recovery; soak not run |
| Web app (Leasyd): overview dashboards, logs explorer, trace view, sign-in; S3 + CloudFront, `/v1/*` proxied to the API | W1 | live, checked with real data through the API |
| Customer quick start (`docs/QUICKSTART.md`): any OpenTelemetry SDK over OTLP/HTTP; gRPC via the Collector | S1 | tested on AWS: Python, Node, Go, Java agent, Collector (gRPC in) |
| Own names: `https://ingest.leasyd.com` (API), `https://app.leasyd.com` (web app); only these two are delegated to Route 53 (`obs-dns`), leasyd.com's website stays at GoDaddy/Vercel | P1 | live; the canary sends through ingest.leasyd.com every minute (sent and found) |
| One-command up/down (`infra/up.sh`, `infra/down.sh`); data in `obs-state` survives a down | P1 | written, not yet run: this account still has the old stack layout (the full down/up was postponed) |
| Metrics explorer: every metric with its type; charts per kind (gauge avg/max/min/p95, counter rate per second from cumulative or delta points, histogram average and rate), split by service or any attribute. Query engine: `increase` aggregate (reset-aware, stitched across workers) | M1 | live; checked on AWS: a known counter sent through ingest reads back exactly (0.70/s, restart handled), histogram average and gauge exact; real load-test data split over 12 deployed workers equals 1 worker |
| Demo tenant `leasyd-demo`: live, realistic shop telemetry every minute (`obs-demo`), 24 h backfill; the canary also sends metrics | D1 | live: 24 h backfilled (4,320 requests, all accepted), 12 services, ~670k spans, 264k logs, 143k metric points a day; queries 2.6-3.5 s |
| Query engine: workers planned by file count too (a week of small files: 27 s -> 6 s, 1.7 s warm); counter increases never split inside a time overlap (was +7% on some days) | M1 | live; checked on the demo week against the generator's exact totals: every day equal |
| Alerting on customer data, billing | - | not started |

Code: `services/` (compaction incl. query engine, ingest, tenants, loadgen), each with `pytest` tests.
Infra: `infra/*.yaml` (one CloudFormation stack per phase), `infra/deploy-*.sh`, `infra/*-test.*`.

## Stacks on AWS

Everything is CloudFormation, brought up and taken down by one command each (`infra/up.sh`,
`infra/down.sh`; see `infra/README.md`). `obs-state` holds data, tenants and logins and survives a
plain `down.sh`; `obs-dns` holds the domain and is never deleted by the scripts. The rest is compute:
`obs-phase0`, `obs-phase2`, `obs-phase3`, `obs-phase4`, `obs-phaseT2`, `obs-phaseT5`, `obs-phaseT7`,
`obs-phaseW1`, plus test tools `obs-phase1` and `obs-phaseT6` (`TEST_TOOLS=1`). `up.sh`/`down.sh`
need admin credentials; `obs-deployer` runs tests. Lambda concurrency limit: 1000.

## Key results (details in the plan)

- 50 GB/h across 100 tenants: every request accepted, freshness p99 44 s (target 60 s),
  ingest + fast lane $0.117/GB then; after compressing before Firehose, $0.077/GB at 10 GB/h.
- Query, largest tenant (1.26 GB/day): 1 day in 1.3 s with 19 workers reading only the needed
  columns (target < 5 s); trace ID across 30 days in 1.0 s (target < 3 s); results identical to Athena.
- Compaction 1.7x faster (profiled: JSON parsing, not the ID loops), identical output.

## Next

1. Freshness at 50 GB/h fixed: p99 38 s (was 74 s) with the fast parse in the fast lane and 15 s
   Firehose buffers for the 10 largest tenants (`infra/tenant.sh tune <tenant> 15`). Details and
   all benchmarks: `docs/REPORT-2026-09-28.md`. Still to do: Lambda memory above 3008 MB (ask AWS), compaction chunks capped by record count
   before testing 100 GB/h.
2. T6 fault tests and a soak run; then delete the 100 `t6-*` tenants
   (`python3 infra/t6/loadtest.py tenants delete`) and the `obs-phaseT6` stack.

## Known limits (customer-facing)

- A new API key is accepted by some API Gateway nodes and refused (403) by others for up to ~10
  minutes (measured 2026-09-28; ~1 min earlier the same day). Existing keys are unaffected. Fix if it
  matters: enforce per-tenant rate limits in the authorizer instead of API Gateway usage plans.
- Ingest is OTLP over HTTP only. gRPC senders and other agents' formats (Prometheus, Fluent Bit, ...)
  go through an OpenTelemetry Collector (`docs/QUICKSTART.md`).
- Invitation emails use Cognito's built-in sender (~50/day); move to Amazon SES before real volume.
- `/v1/app/*` (signed-in users) has no per-tenant rate limit, only the stage throttle.
- Queries over 29 s time out at API Gateway (504). Today: 2-4 s. Fix when needed: asynchronous
  queries (job id, poll for the result).

## Loose ends

- Old test tenants from before tenant records (`probe-*`, `t2a/t2b/t2tiny-*`, `t3-110448`, `t3-110957`,
  `t3dbg`, `t4-120402`, `t4-120927`) are being deleted (2026-09-28; purge passes finish on their own).
  Kept on purpose: `t6-000`..`t6-099` and the `obs-phaseT6` stack (load tests; idle cost ~0),
  `canary` (freshness canary), `seed-acme` / `seed-globex` (T1 isolation test data),
  `_bench/chunk-2m/` (the 2M-record benchmark chunk).
- Phase 0 locked down (2026-09-28): `AllowTestAssume=false EnableLifecycleTest=false`. Platform
  roles are assumable only by their AWS services (tenant reader: only `obs-query`). The Phase 0 / T1
  deny tests need `AllowTestAssume=true` temporarily to run again.
- Pull request #1 merged into `main` (d832b05). New work: same branch, new pull request.

## Findings worth remembering

- New AWS accounts start with a Lambda concurrency limit of 10; raise it before any real traffic.
- New API keys take ~1 minute to work everywhere (they flicker 200/403 meanwhile); revocation ~1 minute.
- API Gateway may decompress gzip bodies but keep `Content-Encoding: gzip`; ingest checks the gzip magic bytes.
- Load testing found and fixed: colliding bloom file names, a race deleting fast-lane blooms mid-lookup,
  compaction failing whenever DuckDB spilled to disk, and slow fast-lane indexing of big files.
