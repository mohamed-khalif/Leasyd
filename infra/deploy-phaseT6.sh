#!/usr/bin/env bash
# Builds and uploads the load generator and deploys obs-phaseT6 (test tooling only:
# nothing here serves customers). Extra arguments go to `cloudformation deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

infra/build-loadgen.sh
aws cloudformation package \
  --template-file infra/phaseT6-loadtest.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseT6 \
  --output-template-file infra/phaseT6-loadtest.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseT6 \
  --template-file infra/phaseT6-loadtest.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=T6 "$@"
