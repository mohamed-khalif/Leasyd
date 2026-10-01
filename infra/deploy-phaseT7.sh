#!/usr/bin/env bash
# Failure visibility (T7): the freshness canary (data, web app, app API), error and "stopped running"
# alarms, email reputation, a monthly cost budget (MONTHLY_BUDGET_USD, default 100) and the
# Leasyd-health dashboard (obs-phaseT7). Deploy after obs-phaseW1 so the web app is checked.
# First run: creates the "canary" tenant and stores its API key in SSM (SecureString
# /obs/canary/api-key); the key never appears in output or in the repo.
# Needs infra/iam/deployer-phaseT7.json on obs-deployer, and Phase 0 + Phase 2 deployed first.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
PARAM=/obs/canary/api-key

if ! aws ssm get-parameter --name "$PARAM" >/dev/null 2>&1; then
  if infra/tenant.sh status canary 2>/dev/null | grep -q '"active"'; then
    key="$(infra/tenant.sh rotate canary 0 2>/dev/null)"
  else
    key="$(infra/tenant.sh create canary 2>/dev/null)"
  fi
  aws ssm put-parameter --name "$PARAM" --type SecureString --value "$key" --overwrite >/dev/null
  unset key
  echo "canary tenant key stored in SSM $PARAM"
fi

ENDPOINT="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)"
WEB_URL="$(aws cloudformation describe-stacks --stack-name obs-phaseW1 \
  --query "Stacks[0].Outputs[?OutputKey=='WebUrl'].OutputValue" --output text 2>/dev/null || true)"
# The budget emails go to ALERT_EMAIL, else to the email address already subscribed to obs-alerts.
ALERT_EMAIL="${ALERT_EMAIL:-$(aws sns list-subscriptions-by-topic \
  --topic-arn "arn:aws:sns:${AWS_DEFAULT_REGION}:${ACCOUNT}:obs-alerts" \
  --query "Subscriptions[?Protocol=='email'].Endpoint | [0]" --output text 2>/dev/null || true)}"
[[ "$WEB_URL" == "None" ]] && WEB_URL=""
[[ "$ALERT_EMAIL" == "None" ]] && ALERT_EMAIL=""
PARAMS=("IngestEndpoint=${ENDPOINT}" "WebUrl=${WEB_URL}" "AlertEmail=${ALERT_EMAIL}")
[[ -n "${MONTHLY_BUDGET_USD:-}" ]] && PARAMS+=("MonthlyBudgetUsd=${MONTHLY_BUDGET_USD}")
infra/build-canary.sh
aws cloudformation package \
  --template-file infra/phaseT7-monitoring.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseT7 \
  --output-template-file infra/phaseT7-monitoring.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseT7 \
  --template-file infra/phaseT7-monitoring.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=T7 \
  --parameter-overrides "${PARAMS[@]}" "$@"
