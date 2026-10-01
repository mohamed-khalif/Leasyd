#!/usr/bin/env bash
# Deploys the AI SRE (obs-phaseAI). Pass the Claude Platform on AWS workspace and its region:
#   CLAUDE_WORKSPACE_ID=wrkspc_... CLAUDE_REGION=us-east-1 infra/deploy-phaseAI.sh
# (later runs reuse the values already deployed). Needs obs-phase0, obs-phase4, obs-phaseT2.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
t2() { aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
PARAMS=("ApiId=$(t2 ApiId)")
[[ -n "${CLAUDE_WORKSPACE_ID:-}" ]] && PARAMS+=("ClaudeWorkspaceId=${CLAUDE_WORKSPACE_ID}")
[[ -n "${CLAUDE_REGION:-}" ]] && PARAMS+=("ClaudeRegion=${CLAUDE_REGION}")

infra/build-ai.sh
infra/data-bucket-rules.sh      # incl. the 30-day expiry of AI conversations (_ai/)
aws cloudformation package \
  --template-file infra/phaseAI-sre.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseAI \
  --output-template-file infra/phaseAI-sre.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseAI \
  --template-file infra/phaseAI-sre.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=AI \
  --parameter-overrides "${PARAMS[@]}" "$@"
