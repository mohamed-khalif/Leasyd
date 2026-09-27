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
infra/phase1-test.sh obs-phase1
```

The collector costs about $0.60/day while running. Stop it between tests with
`--parameter-overrides CollectorDesiredCount=0`.

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
