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
  --parameter-overrides BudgetEmail=you@example.com \
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
