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

# Investigation emails to the account's own users go by SES from the obs-phaseE1 sender, once it exists.
EMAIL_FROM="$(aws cloudformation describe-stacks --stack-name obs-phaseE1 \
  --query "Stacks[0].Outputs[?OutputKey=='EmailFrom'].OutputValue" --output text 2>/dev/null || true)"
EXTRA=()
[[ -n "$EMAIL_FROM" && "$EMAIL_FROM" != None ]] && EXTRA=("EmailFrom=${EMAIL_FROM}")

infra/build-investigator.sh
aws cloudformation package \
  --template-file infra/phaseI1-investigator.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseI1 \
  --output-template-file infra/phaseI1-investigator.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseI1 \
  --template-file infra/phaseI1-investigator.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=I1 \
  --parameter-overrides "ApiId=${API_ID}" ${EXTRA[@]+"${EXTRA[@]}"} "$@"
