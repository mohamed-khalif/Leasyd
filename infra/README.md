# Infrastructure

## Phase 0: foundation

`phase0-foundation.yaml` creates the data bucket, the lifecycle rules, the `obs-boundary`
permissions boundary, the `obs-collector` / `obs-compaction` / `obs-query` roles and a
monthly budget.

Deploy:

```bash
aws cloudformation deploy \
  --stack-name obs-phase0 \
  --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags project=obs phase=0
```

Test:

```bash
infra/phase0-test.sh obs-phase0              # IAM allow/deny checks + lifecycle probe upload
infra/phase0-test.sh obs-phase0 lifecycle    # 1-2 days later: probe should be GLACIER_IR
```

After the tests pass, redeploy with `AllowTestAssume=false EnableLifecycleTest=false`
so the roles can only be assumed by their AWS services.

The bucket is retained if the stack is deleted, so data is never removed by accident.

## Phase 1: write path

`phase1-write-path.yaml` creates a small VPC (public subnets, no NAT, S3 gateway endpoint),
an ECS cluster running the OpenTelemetry Collector (0.161.0) on Fargate, a `telemetrygen`
load-generator task, and the `obs.raw_logs` Glue table + `obs` Athena workgroup.
Phase 0 must be redeployed first: it now enables S3 request metrics on `_incoming/`.

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=0
aws cloudformation deploy --stack-name obs-phase1 --template-file infra/phase1-write-path.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=1
```

Since Phase T2, ingest is serverless (see below) and the collector has been removed from this
stack; it now holds only the load generator and the Athena tables. Phase 1's original test,
which sent traffic straight to a collector task, was retired; `infra/phaseT2-test.sh` covers
ingest.

## Phase 2: compaction

`phase2-compaction.yaml` creates the `obs-index` table, the `obs-compaction-dispatcher`
(every 15 min) and `obs-compaction-worker` Lambdas, an `obs-compaction-stuck` alarm
emailing AWSkhalif@gmail.com, and the `obs.logs` Athena table over the Parquet.
Code and tests are in `services/compaction/`
(`pip install -r requirements-dev.txt && pytest`).

Phase 0 must be redeployed first: it adds the artifacts bucket and lets the compaction
role delete index items and invoke the worker. The deployer also needs
`infra/iam/deployer-phase2.json`.

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=DISABLED
infra/phase2-test.sh load 3          # then wait until one full hour has closed (+10 min)
infra/phase2-test.sh compare
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
```

Confirm the SNS subscription email so the stuck-compaction alarm can reach you.

## Phase 3: index lookup

Compaction now builds a bloom filter per Parquet file over `trace_id` and `request.id`
(set `BloomAttributes` on the Phase 2 stack to change the attributes), and records every
service seen. `phase3-index.yaml` adds `obs-index-lookup`, a Lambda that returns the files
a query needs for given services, a time range and an optional trace or request ID. It
shares the Lambda bundle in `services/compaction/`.

```bash
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phase3.sh
infra/phase3-test.sh
```

Files compacted before this change have no bloom filter; lookups never skip them.

## Phase T1: tenant-aware storage, compaction and lookups

Every path and index key now starts with a tenant (see `services/compaction/layout.py`):
raw data under `_incoming/tenant=<T>/`, Parquet under `data/tenant=<T>/`, index keys
`<T>#...`. The query role no longer reads data itself; lookups assume `obs-tenant-reader`
with the tenant as a session tag, and IAM limits that session to the tenant's prefix and
index keys. Until authenticated ingest (T2), the collector files everything under tenant
`default`. Athena tables now need `WHERE tenant = '...'`.

Deploy in this order. **Phase 0 must be deployed with admin credentials**: it changes the
`obs-boundary` permissions boundary, which `obs-deployer` is deliberately not allowed to
modify. Also attach `infra/iam/deployer-phaseT.json` to `obs-deployer` for the tests.

```bash
AWS_PROFILE=<admin> aws cloudformation deploy --stack-name obs-phase0 \
  --template-file infra/phase0-foundation.yaml --capabilities CAPABILITY_NAMED_IAM
aws cloudformation deploy --stack-name obs-phase1 --template-file infra/phase1-write-path.yaml \
  --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phase3.sh
infra/phase0-test.sh obs-phase0      # now includes cross-tenant deny checks
infra/phase3-test.sh                 # lookups, including tenant isolation
```

Data written before this change (`logs/`, `_incoming/logs/`, index keys without a tenant)
is no longer read or compacted; it's test data and can be deleted.

## Phase T2: authenticated, serverless ingest

Customers send OTLP/HTTP to a public HTTPS endpoint with an `x-api-key` header:

```
OTEL_EXPORTER_OTLP_ENDPOINT=<IngestEndpoint output of obs-phaseT2>
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=<key>
```

```
SDK --HTTPS--> API Gateway --> authorizer Lambda (key -> tenant, obs-tenants table)
                   |              usage plan: per-tenant rate limit
                   v
               obs-ingest Lambda --> Firehose obs-t-<tenant>-<signal> --> s3://.../_incoming/tenant=<T>/<signal>/
```

- Nothing runs, or costs, while idle. Cost is per request (API Gateway, Lambda) and per GB
  (Firehose, $0.029/GB; each record is billed as at least 5 KB).
- The tenant comes only from the authorizer; client `obs.*` resource attributes are stripped.
- A client gets 200 only once Firehose has stored the records durably; otherwise 503, and its
  SDK retries (at least once: a retry after a partial failure can duplicate records).
- Each tenant has its own Firehose stream per signal (created by `infra/tenant.sh`), so no
  dynamic-partitioning fees or shared partition limits, and per-tenant throughput limits.
  Streams flush every 30 s (`BUFFER_SECONDS`) or at 64 MB.
- Requests over about 4.5 MB are rejected (Lambda's 6 MB limit applies after API Gateway base64-encodes the body); SDK batches are far smaller.

**Phase 0 must be redeployed with admin credentials** (the boundary gains Firehose puts and
Firehose's S3 delivery actions). Attach `infra/iam/deployer-phaseT2.json` to `obs-deployer`. Then:

```bash
AWS_PROFILE=<admin> aws cloudformation deploy --stack-name obs-phase0 \
  --template-file infra/phase0-foundation.yaml --capabilities CAPABILITY_NAMED_IAM
aws cloudformation deploy --stack-name obs-phase1 --template-file infra/phase1-write-path.yaml \
  --capabilities CAPABILITY_NAMED_IAM          # removes the old collector
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phaseT2.sh
infra/phaseT2-test.sh
```

Onboard a tenant: `infra/tenant.sh create acme` (creates its streams, prints the key once).
Revoke: `infra/tenant.sh revoke acme`.

## Phase T3: fast lane (searchable within about a minute)

Each new raw file (Firehose flushes every 30 s) triggers `obs-recent-indexer` through an S3
event on EventBridge. It indexes the file as `kind=raw` entries, one per (service, event
hour) with time range, row count and bloom filter, using the same parsing code as compaction.
`obs-index-lookup` returns raw and Parquet files together, marked by `kind`.

Handover: a chunk's compaction plan decides which copy lookups show. While the plan is
`planned`, raw entries are visible and its new Parquet entries hidden; once it's `committed`
(a single write), Parquet is visible and those raw entries hidden; cleanup then deletes the raw
entries and files. So a query never counts a record twice or misses it, whatever step a crash
interrupts (tested in `services/compaction/test_fastlane.py`).

Plan records moved to `<T>#_plan#...` so a tenant's lookups can read them; the tenant reader
can now also read that tenant's raw files. No plans may be in flight when deploying this
(`aws dynamodb scan --table-name obs-index --filter-expression 'begins_with(pk, :p)'
--expression-attribute-values '{":p":{"S":"_plan#"}}' --select COUNT` should be 0).

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phase3.sh
infra/phaseT3-test.sh
```
