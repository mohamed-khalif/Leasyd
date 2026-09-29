#!/usr/bin/env bash
# Customer logins (U1): the Cognito user pool obs-users and its app client (obs-phaseU1).
# Then redeploy obs-phaseT2 (routes /v1/app/*) and obs-phaseT5 (invite-user).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
aws cloudformation deploy --stack-name obs-phaseU1 \
  --template-file infra/phaseU1-users.yaml --tags project=obs phase=U1 "$@"
aws cloudformation describe-stacks --stack-name obs-phaseU1 --query "Stacks[0].Outputs" --output table
