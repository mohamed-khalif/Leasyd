#!/usr/bin/env bash
# Soak test customer (obs-soak): an EC2 instance running the OpenTelemetry Demo, sending to
# ingest.leasyd.com as tenant qa-soak-2, plus the exact ledger (infra/t6/soak.py) as qa-soak-1.
# Needs the keys in SSM /obs/soak/customer-key and /obs/soak/ledger-key (soak.py setup makes them).
#   infra/deploy-soak.sh [Hours=12 LocustUsers=25]   start (the instance stops itself after Hours)
#   infra/deploy-soak.sh down                         empty the bucket and delete the stack
# Cost: t3.xlarge ~$0.17/h plus the data it sends.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:=us-east-1}"
export AWS_DEFAULT_REGION
out() { aws cloudformation describe-stacks --stack-name obs-soak \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

if [[ "${1:-}" == down ]]; then
  aws s3 rm "s3://$(out Bucket)" --recursive --quiet
  aws cloudformation delete-stack --stack-name obs-soak
  aws cloudformation wait stack-delete-complete --stack-name obs-soak
  echo "obs-soak deleted"
  exit 0
fi

OVERRIDES=(); [[ $# -gt 0 ]] && OVERRIDES=(--parameter-overrides "$@")
aws cloudformation deploy --stack-name obs-soak --template-file infra/soak-customer.yaml \
  --capabilities CAPABILITY_IAM --tags project=obs phase=soak "${OVERRIDES[@]}"
aws s3 cp infra/t6/soak.py "s3://$(out Bucket)/soak.py" --quiet
echo "obs-soak started: instance $(out InstanceId), results in s3://$(out Bucket)/ (demo up in ~10 min)"
