#!/usr/bin/env bash
# The platform's own email (obs-phaseE1): an SES domain identity for app.<domain> with DKIM, its
# records in obs-dns's app.<domain> zone. Then redeploy obs-phaseT5, which sends from it.
#
# New AWS accounts' SES is in the "sandbox": it only sends to addresses verified in SES, at most 200
# a day. For real customers, ask AWS for production access once (SES console > Account dashboard >
# Request production access; usually answered within a day). Until then, verify your own address
# to try it: aws sesv2 create-email-identity --email-identity you@example.com
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
dns() { aws cloudformation describe-stacks --stack-name obs-dns \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
DOMAIN="$(dns DomainName)"
aws cloudformation deploy --stack-name obs-phaseE1 --template-file infra/phaseE1-email.yaml \
  --tags project=obs phase=E1 \
  --parameter-overrides "MailDomain=app.${DOMAIN}" "HostedZoneId=$(dns AppZoneId)" "$@"
echo "Waiting for SES to see the DKIM records (usually a few minutes)..."
for _ in $(seq 1 60); do
  status="$(aws sesv2 get-email-identity --email-identity "app.${DOMAIN}" \
    --query 'DkimAttributes.Status' --output text)"
  [[ "$status" == SUCCESS ]] && break
  sleep 15
done
echo "DKIM: ${status}"
aws sesv2 get-account --query '{production_access: ProductionAccessEnabled, sending: SendingEnabled, max_24h: SendQuota.Max24HourSend}'
