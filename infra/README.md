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

(Phase 1's original test sent unauthenticated traffic straight to a collector task; since
Phase T2 the collector drops anything without a gateway-assigned tenant, so it was retired.
`infra/phaseT2-test.sh` and `infra/test-collector-config.sh` cover ingest.)

The collector costs about $1.20/day per task while running (2 tasks minimum since T2), plus
about $0.60/day for the load balancer. Stop the tasks between tests with
`--parameter-overrides CollectorMinTasks=0`.

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

## Phase T2: authenticated, durable, autoscaling ingest

Customers send OTLP/HTTP to a public HTTPS endpoint with an `x-api-key` header:

```
OTEL_EXPORTER_OTLP_ENDPOINT=<IngestEndpoint output of obs-phaseT2>
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=<key>
```

- **API Gateway** (`phaseT2-ingest.yaml`): a Lambda authorizer maps the key's SHA-256 to a
  tenant (`obs-tenants` table) and the gateway sets `x-obs-tenant` for the backend. Usage
  plans limit each tenant's request rate. Keys are managed with `infra/tenant.sh`.
- **Collector** (`phase1-write-path.yaml`): at least 2 tasks across two AZs behind an internal
  NLB, CPU autoscaling up to `CollectorMaxTasks`. The tenant is taken only from the gateway's
  header (a client's own `obs.tenant` is deleted first; no header means the data is dropped).
  Batches are per tenant and capped at 8 MB. A client is answered only once its batch is in
  S3, so if a task dies the client's SDK retries instead of the data being lost.
- `infra/test-collector-config.sh` runs the exact collector config from the template locally
  against the real bucket and checks tenant stamping, spoofing, dropping, ack-after-write,
  SIGKILL and SIGTERM behaviour.

Attach `infra/iam/deployer-phaseT2.json` to `obs-deployer` first. Then:

```bash
aws cloudformation deploy --stack-name obs-phase1 --template-file infra/phase1-write-path.yaml \
  --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phaseT2.sh
infra/test-collector-config.sh
infra/phaseT2-test.sh
```

Onboard a tenant: `infra/tenant.sh create acme` (prints the key once). Revoke: `infra/tenant.sh revoke acme`.
