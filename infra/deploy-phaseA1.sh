#!/usr/bin/env bash
# Alerts (A1): builds the bundle (alerts + the address checks + ingest's OTLP-to-Firehose code) and
# deploys obs-phaseA1. Needs obs-phase0 (its boundary allows the customer email topics), obs-phase4
# and obs-phaseS1. Deploy it before obs-phaseT2, whose /v1/app/alerts and /v1/app/dashboards routes invoke obs-alerts.
# Extra arguments go to `cloudformation deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
# Links in alerts point at the portal: app.<domain> once obs-dns exists.
DOMAIN="$(aws cloudformation describe-stacks --stack-name obs-dns --query "Stacks[0].Outputs[?OutputKey=='DomainName'].OutputValue" --output text 2>/dev/null || true)"
APP_ARGS=()
[[ -n "$DOMAIN" && "$DOMAIN" != None ]] && APP_ARGS=("AppUrl=https://app.${DOMAIN}")

rm -rf services/alerts/build && mkdir -p services/alerts/build
pip install -q --target services/alerts/build --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 -r services/ingest/requirements.txt
cp services/alerts/alerts.py services/alerts/dashboards.py services/synthetics/safety.py \
  services/ingest/ingest.py services/ingest/cloudwatch.py services/ingest/droprules.py services/alerts/build/
python3 infra/check-bundle.py services/alerts/build
aws cloudformation package \
  --template-file infra/phaseA1-alerts.yaml \
  --s3-bucket "obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}" --s3-prefix phaseA1 \
  --output-template-file infra/phaseA1-alerts.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseA1 \
  --template-file infra/phaseA1-alerts.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=A1 \
  ${APP_ARGS[@]+--parameter-overrides "${APP_ARGS[@]}"} "$@"
