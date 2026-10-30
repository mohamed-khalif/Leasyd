#!/usr/bin/env bash
# Automatic investigations (obs-phaseI1): the queue, obs-investigator (alerts and webhooks open
# investigations; the app reads them) and obs-investigator-worker. Needs obs-phase0 redeployed first
# (boundary: InvestigationQueue). Then redeploy obs-phaseT2 (the routes) and obs-phaseA1 (firing rules
# start investigations). Extra arguments go to `cloudformation deploy` (e.g. --parameter-overrides HourlyCap=10).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
API_ID="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 --query "Stacks[0].Outputs[?OutputKey=='ApiId'].OutputValue" --output text)"

infra/build-investigator.sh
aws cloudformation package \
  --template-file infra/phaseI1-investigator.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseI1 \
  --output-template-file infra/phaseI1-investigator.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseI1 \
  --template-file infra/phaseI1-investigator.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=I1 \
  --parameter-overrides "ApiId=${API_ID}" "$@"
