#!/usr/bin/env bash
# Builds and uploads the tenant-admin code and deploys obs-phaseT5.
# Extra arguments go to `cloudformation deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
t2() { aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
u1() { aws cloudformation describe-stacks --stack-name obs-phaseU1 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true; }

infra/build-tenants.sh
aws cloudformation package \
  --template-file infra/phaseT5-tenants.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseT5 \
  --output-template-file infra/phaseT5-tenants.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseT5 \
  --template-file infra/phaseT5-tenants.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=T5 \
  --parameter-overrides "StandardPlanId=$(t2 StandardPlanId)" "TestTinyPlanId=$(t2 TestTinyPlanId)" \
                        "UserPoolId=$(u1 UserPoolId)" "$@"
