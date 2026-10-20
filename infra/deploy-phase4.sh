#!/usr/bin/env bash
# Builds and uploads the shared Lambda bundle and deploys obs-phase4 (query engine).
# Extra arguments go to `cloudformation deploy`. When obs-phaseH1 (the hot store) exists, obs-query
# joins its VPC and gets its URL; which accounts use it is HotAccounts (docs/hot-store.md).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

infra/build-compaction.sh
infra/data-bucket-rules.sh      # incl. the 1-day expiry of long queries' answers (_results/)
aws cloudformation package \
  --template-file infra/phase4-query.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phase4 \
  --output-template-file infra/phase4-query.packaged.yaml
ARGS=("$@")
if HOT_URL="$(aws cloudformation describe-stacks --stack-name obs-phaseH1 \
    --query "Stacks[0].Outputs[?OutputKey=='HotUrl'].OutputValue" --output text 2>/dev/null)"; then
  h1() { aws cloudformation describe-stacks --stack-name obs-phaseH1 --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
  HOT=("HotUrl=${HOT_URL}" "HotSubnets=$(h1 QuerySubnets)" "HotSecurityGroup=$(h1 QuerySecurityGroup)")
  ARGS=()
  for a in "$@"; do ARGS+=("$a"); [[ "$a" == --parameter-overrides ]] && ARGS+=("${HOT[@]}"); done
  [[ " $* " == *" --parameter-overrides "* ]] || ARGS+=(--parameter-overrides "${HOT[@]}")
fi
aws cloudformation deploy --stack-name obs-phase4 \
  --template-file infra/phase4-query.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=4 "${ARGS[@]}"
