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
# The Glue database "obs": this stack's, except in accounts set up before 29 Sep 2026, where
# obs-phase1 made it (creating it again fails: "Database already exists").
OWN_GLUE="$(aws cloudformation describe-stacks --stack-name obs-phase2 \
  --query "Stacks[0].Outputs[?OutputKey=='GlueDatabaseOwned'].OutputValue" --output text 2>/dev/null || true)"
if [[ "$*" == *CreateGlueDatabase=* ]]; then
  OWN_GLUE=""                                       # given by the caller
elif [[ "$OWN_GLUE" != true && "$OWN_GLUE" != false ]]; then
  if ! aws glue get-database --name obs >/dev/null 2>&1; then
    OWN_GLUE=true                                   # a new account: this stack makes it
  elif out="$(aws cloudformation describe-stack-resource --stack-name obs-phase2 \
                --logical-resource-id GlueDatabase 2>&1)"; then
    OWN_GLUE=true                                   # already this stack's
  elif grep -q "does not exist" <<<"$out"; then
    OWN_GLUE=false                                  # made elsewhere: leave it there
  else
    echo "can't tell who owns the Glue database obs: $out" >&2
    echo "re-run with: $0 --parameter-overrides CreateGlueDatabase=false  (or true if obs-phase2 made it)" >&2
    exit 1
  fi
fi
# One --parameter-overrides list (a second one would replace it): ours and the caller's.
ARGS=() OVERRIDES=(${OWN_GLUE:+"CreateGlueDatabase=$OWN_GLUE"}) in_overrides=0
for a in "$@"; do
  if [[ "$a" == --parameter-overrides ]]; then in_overrides=1
  elif (( in_overrides )) && [[ "$a" != --* ]]; then OVERRIDES+=("$a")
  else in_overrides=0; ARGS+=("$a"); fi
done
aws cloudformation deploy --stack-name obs-phase2 \
  --template-file infra/phase2-compaction.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=2 \
  ${OVERRIDES[@]:+--parameter-overrides "${OVERRIDES[@]}"} "${ARGS[@]}"
