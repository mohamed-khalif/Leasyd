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
