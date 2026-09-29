#!/usr/bin/env bash
# Demo tenant data (D1): obs-demo sends a realistic online shop's telemetry for "leasyd-demo" every
# minute (obs-phaseD1). First run: creates the tenant, stores its ingest key in SSM
# (SecureString /obs/demo/api-key; never printed) and backfills the last 24 hours.
# Needs obs-phaseT2 and obs-phaseT5. Then give someone a login: infra/tenant.sh invite-user leasyd-demo <email>
# Pause it: infra/deploy-phaseD1.sh --parameter-overrides State=DISABLED
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
TENANT=leasyd-demo
PARAM=/obs/demo/api-key
[[ "$(aws --version 2>&1)" == aws-cli/1.* ]] && PAYLOAD_FMT=() || PAYLOAD_FMT=(--cli-binary-format raw-in-base64-out)

FIRST=""
if ! aws ssm get-parameter --name "$PARAM" >/dev/null 2>&1; then
  if infra/tenant.sh status "$TENANT" 2>/dev/null | grep -q '"active"'; then
    key="$(infra/tenant.sh rotate "$TENANT" 0 2>/dev/null)"
  else
    key="$(infra/tenant.sh create "$TENANT" 2>/dev/null)"
  fi
  aws ssm put-parameter --name "$PARAM" --type SecureString --value "$key" --overwrite >/dev/null
  unset key
  echo "$TENANT tenant key stored in SSM $PARAM"
  FIRST=1
fi

ENDPOINT="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)"
rm -rf services/demo/build && mkdir -p services/demo/build && cp services/demo/demo.py services/demo/build/
aws cloudformation package \
  --template-file infra/phaseD1-demo.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseD1 \
  --output-template-file infra/phaseD1-demo.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseD1 \
  --template-file infra/phaseD1-demo.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=D1 \
  --parameter-overrides "IngestEndpoint=${ENDPOINT}" "$@"

if [[ -n "$FIRST" || -n "${BACKFILL_HOURS:-}" ]]; then
  aws lambda invoke --function-name obs-demo --invocation-type Event "${PAYLOAD_FMT[@]}" \
    --payload "{\"backfill_hours\": ${BACKFILL_HOURS:-24}}" /dev/null >/dev/null
  echo "backfilling the last ${BACKFILL_HOURS:-24} hours of demo data (a new key can take ~10 minutes to be accepted;"
  echo "the backfill waits for it). Live data arrives every minute."
fi
