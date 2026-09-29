#!/usr/bin/env bash
# Builds and uploads the ingest + authorizer code and deploys obs-phaseT2.
# Extra arguments go to `cloudformation deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

infra/build-ingest.sh
aws cloudformation package \
  --template-file infra/phaseT2-ingest.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseT2 \
  --output-template-file infra/phaseT2-ingest.packaged.yaml
# Customer logins (/v1/app/*): the user pool in obs-state.
POOL_ARN="$(aws cloudformation describe-stacks --stack-name obs-state \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolArn'].OutputValue" --output text 2>/dev/null || true)"
# The public name ingest.<domain>, once obs-dns has its certificate (infra/deploy-dns.sh).
dns() { aws cloudformation describe-stacks --stack-name obs-dns \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true; }
DOMAIN_ARGS=()
CERT="$(dns CertificateArn)"
if [[ -n "$CERT" && "$CERT" != None ]]; then
  DOMAIN_ARGS=("ApiHostName=ingest.$(dns DomainName)" "CertificateArn=${CERT}" "HostedZoneId=$(dns HostedZoneId)")
fi
aws cloudformation deploy --stack-name obs-phaseT2 \
  --template-file infra/phaseT2-ingest.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=T2 \
  --parameter-overrides "UserPoolArn=${POOL_ARN}" "${DOMAIN_ARGS[@]}" "$@"
