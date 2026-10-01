#!/usr/bin/env bash
# Deploys obs-phase0: the permissions boundary, the platform's shared roles (incl. obs-query's
# per-tenant limits and the AI SRE's Bedrock access) and the monthly cost budget
# (obs-account-monthly). Needs admin rights (it changes IAM). Parameters already deployed
# (BudgetEmail, BudgetLimitUsd, ...) are kept; pass --parameter-overrides to change them.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=0 --no-fail-on-empty-changeset "$@"
aws cloudformation describe-stacks --stack-name obs-phase0 --query "Stacks[0].[StackStatus,LastUpdatedTime]" --output text
