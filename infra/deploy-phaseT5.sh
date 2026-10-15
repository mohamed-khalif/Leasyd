#!/usr/bin/env bash
# Builds and uploads the tenant-admin code and deploys obs-phaseT5.
# Extra arguments go to `cloudformation deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
t2() { aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
# Logins: obs-state (obs-phaseU1 in accounts not yet moved to obs-state).
LOGINS=obs-state; aws cloudformation describe-stacks --stack-name obs-state >/dev/null 2>&1 || LOGINS=obs-phaseU1
u1() { aws cloudformation describe-stacks --stack-name "$LOGINS" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true; }

# Invitations and sign-up emails from the SES domain of obs-phaseE1, once it's verified.
EMAIL_FROM="$(aws cloudformation describe-stacks --stack-name obs-phaseE1 \
  --query "Stacks[0].Outputs[?OutputKey=='EmailFrom'].OutputValue" --output text 2>/dev/null || true)"
[[ "$EMAIL_FROM" == None ]] && EMAIL_FROM=""

infra/build-tenants.sh
infra/data-bucket-rules.sh      # retention backstop (and screenshots) on the data bucket
aws cloudformation package \
  --template-file infra/phaseT5-tenants.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseT5 \
  --output-template-file infra/phaseT5-tenants.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseT5 \
  --template-file infra/phaseT5-tenants.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=T5 \
  --parameter-overrides "StandardPlanId=$(t2 StandardPlanId)" "TestTinyPlanId=$(t2 TestTinyPlanId)" \
                        "FreePlanId=$(t2 FreePlanId)" "ApiId=$(t2 ApiId)" \
                        "UserPoolId=$(u1 UserPoolId)" "EmailFrom=${EMAIL_FROM}" "$@"
