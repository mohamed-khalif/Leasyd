#!/usr/bin/env bash
# Builds and uploads the shared Lambda bundle and deploys obs-phase4 (query engine).
# Extra arguments go to `cloudformation deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

infra/build-compaction.sh
aws cloudformation package \
  --template-file infra/phase4-query.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phase4 \
  --output-template-file infra/phase4-query.packaged.yaml
aws cloudformation deploy --stack-name obs-phase4 \
  --template-file infra/phase4-query.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=4 "$@"
