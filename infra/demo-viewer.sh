#!/usr/bin/env bash
# The live demo's login: a read-only "viewer" of the demo tenant (leasyd-demo), never emailed, its
# password in SSM SecureString /obs/demo/viewer-password and its name in /obs/demo/viewer-user.
# Visitors never see the password: POST /v1/demo/session (obs-account-api) signs in for them and
# hands out only a 15-minute ID token. Also lifts the demo tenant's daily search allowance, which
# all demo visitors share. Safe to re-run (keeps an existing login and password).
# Then: infra/deploy-phaseT5.sh (reads the user name), and keep obs-demo-schedule enabled.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:=us-east-1}"
export AWS_DEFAULT_REGION
TENANT="${DEMO_TENANT:-leasyd-demo}"
USER_NAME="${DEMO_VIEWER:-demo@leasyd.com}"
PW_PARAM=/obs/demo/viewer-password

LOGINS=obs-state; aws cloudformation describe-stacks --stack-name obs-state >/dev/null 2>&1 || LOGINS=obs-phaseU1
POOL="$(aws cloudformation describe-stacks --stack-name "$LOGINS" --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)"
admin() {
  out="$(mktemp)"
  aws lambda invoke --function-name obs-tenant-admin --cli-binary-format raw-in-base64-out --payload "$1" "$out" >/dev/null
  cat "$out"; echo; rm -f "$out"
}

if ! aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$USER_NAME" >/dev/null 2>&1; then
  admin "{\"action\":\"invite-user\",\"tenant\":\"${TENANT}\",\"email\":\"${USER_NAME}\",\"role\":\"viewer\",\"send_email\":false}"
fi
if ! aws ssm get-parameter --name "$PW_PARAM" >/dev/null 2>&1; then
  pw="Dv-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')-7k"
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$USER_NAME" --password "$pw" --permanent
  aws ssm put-parameter --name "$PW_PARAM" --type SecureString --value "$pw" --overwrite >/dev/null
  unset pw
fi
aws ssm put-parameter --name /obs/demo/viewer-user --type String --value "$USER_NAME" --overwrite >/dev/null
admin "{\"action\":\"set-search\",\"tenant\":\"${TENANT}\",\"units_per_day\":0}"
echo "live demo login ${USER_NAME} (viewer of ${TENANT}) ready; password in SSM ${PW_PARAM}"
