#!/usr/bin/env bash
# Builds the compaction Lambda, uploads it, and deploys obs-phase2.
# Extra arguments are passed to `cloudformation deploy`, e.g.
#   infra/deploy-phase2.sh --parameter-overrides ScheduleState=DISABLED
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

infra/build-compaction.sh
aws cloudformation package \
  --template-file infra/phase2-compaction.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phase2 \
  --output-template-file infra/phase2-compaction.packaged.yaml
aws cloudformation deploy --stack-name obs-phase2 \
  --template-file infra/phase2-compaction.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=2 "$@"
