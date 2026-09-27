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
aws cloudformation deploy --stack-name obs-phaseT2 \
  --template-file infra/phaseT2-ingest.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=T2 "$@"
