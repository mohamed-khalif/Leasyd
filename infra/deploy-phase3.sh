#!/usr/bin/env bash
# Builds the Lambda bundle (shared with compaction), uploads it, and deploys obs-phase3.
# Extra arguments are passed to `cloudformation deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

infra/build-compaction.sh
aws cloudformation package \
  --template-file infra/phase3-index.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phase3 \
  --output-template-file infra/phase3-index.packaged.yaml
aws cloudformation deploy --stack-name obs-phase3 \
  --template-file infra/phase3-index.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=3 "$@"
