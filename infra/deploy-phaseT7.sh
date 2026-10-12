#!/usr/bin/env bash
# Failure visibility (T7): the freshness canary (data, web app, app API), a signed-in check of every
# app API backend every 5 minutes (obs-app-routes), error and "stopped running"
# alarms, email reputation and the Leasyd-health dashboard (obs-phaseT7). Deploy after obs-phaseW1 so the web app is checked.
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

# The route check signs in as the canary tenant's own login (a member; never emailed), its password
# in SSM SecureString /obs/canary/app-password.
LOGINS=obs-state; aws cloudformation describe-stacks --stack-name obs-state >/dev/null 2>&1 || LOGINS=obs-phaseU1
login_out() { aws cloudformation describe-stacks --stack-name "$LOGINS" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
POOL="$(login_out UserPoolId)"; CLIENT="$(login_out AppClientId)"
APP_USER=canary-app@leasyd.com; APP_PARAM=/obs/canary/app-password
if [[ -n "$CLIENT" && "$CLIENT" != None ]] && ! aws ssm get-parameter --name "$APP_PARAM" >/dev/null 2>&1; then
  if ! aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$APP_USER" >/dev/null 2>&1; then
    out="$(mktemp)"
    aws lambda invoke --function-name obs-tenant-admin --cli-binary-format raw-in-base64-out --payload \
      "{\"action\":\"invite-user\",\"tenant\":\"canary\",\"email\":\"${APP_USER}\",\"role\":\"member\",\"send_email\":false}" "$out" >/dev/null
    rm -f "$out"
  fi
  pw="Cn-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')-9a"
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$APP_USER" --password "$pw" --permanent
  aws ssm put-parameter --name "$APP_PARAM" --type SecureString --value "$pw" --overwrite >/dev/null
  unset pw
  echo "canary login ${APP_USER} ready; password stored in SSM ${APP_PARAM}"
fi

ENDPOINT="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)"
WEB_URL="$(aws cloudformation describe-stacks --stack-name obs-phaseW1 \
  --query "Stacks[0].Outputs[?OutputKey=='WebUrl'].OutputValue" --output text 2>/dev/null || true)"
[[ "$WEB_URL" == "None" ]] && WEB_URL=""
PARAMS=("IngestEndpoint=${ENDPOINT}" "WebUrl=${WEB_URL}")
[[ -n "$CLIENT" && "$CLIENT" != None ]] && PARAMS+=("AppClientId=${CLIENT}" "CanaryAppUser=${APP_USER}")
infra/build-canary.sh
aws cloudformation package \
  --template-file infra/phaseT7-monitoring.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseT7 \
  --output-template-file infra/phaseT7-monitoring.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseT7 \
  --template-file infra/phaseT7-monitoring.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=T7 \
  --parameter-overrides "${PARAMS[@]}" "$@"
