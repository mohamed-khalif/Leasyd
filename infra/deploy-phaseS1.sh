#!/usr/bin/env bash
# Synthetic HTTP checks (S1): builds the bundle (synthetics + ingest's OTLP-to-Firehose code) and
# deploys obs-phaseS1. Deploy it before obs-phaseT2, whose /v1/app/checks routes invoke
# obs-synthetics-api. Extra arguments go to `cloudformation deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

rm -rf services/synthetics/build && mkdir -p services/synthetics/build
pip install -q --target services/synthetics/build --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 -r services/ingest/requirements.txt
cp services/synthetics/synthetics.py services/ingest/ingest.py services/synthetics/build/
aws cloudformation package \
  --template-file infra/phaseS1-synthetics.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseS1 \
  --output-template-file infra/phaseS1-synthetics.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseS1 \
  --template-file infra/phaseS1-synthetics.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=S1 "$@"
