#!/usr/bin/env bash
# Leasyd web app (W1): deploys obs-phaseW1 (S3 + CloudFront, /v1/* routed to the API), builds
# services/web, writes its config.json from the other stacks, uploads it and refreshes CloudFront.
# Needs Node.js 18+ (CloudShell has it) and obs-state + obs-phaseT2 deployed.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
out() { aws cloudformation describe-stacks --stack-name "$1" \
  --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }

API_URL="$(out obs-phaseT2 ApiUrl)"                     # https://<api>.execute-api.<region>.amazonaws.com/<stage>
API_DOMAIN="$(echo "$API_URL" | sed -E 's#https://([^/]+)/.*#\1#')"
STAGE_PATH="/$(echo "$API_URL" | sed -E 's#https://[^/]+/##')"
# The public name app.<domain>, once obs-dns has its certificate (infra/deploy-dns.sh).
DOMAIN_ARGS=()
CERT="$(out obs-dns CertificateArn 2>/dev/null || true)"
if [[ -n "$CERT" && "$CERT" != None ]]; then
  DOMAIN_ARGS=("AppHostName=app.$(out obs-dns DomainName)" "CertificateArn=${CERT}" "HostedZoneId=$(out obs-dns AppZoneId)")
fi
aws cloudformation deploy --stack-name obs-phaseW1 --template-file infra/phaseW1-web.yaml \
  --tags project=obs phase=W1 --parameter-overrides "ApiDomain=${API_DOMAIN}" "ApiStagePath=${STAGE_PATH}" \
  "${DOMAIN_ARGS[@]}" "$@"

BUCKET="$(out obs-phaseW1 WebBucketName)"
DIST="$(out obs-phaseW1 DistributionId)"
URL="$(out obs-phaseW1 WebUrl)"

# Logins: obs-state (obs-phaseU1 in accounts not yet moved to obs-state).
LOGINS=obs-state; aws cloudformation describe-stacks --stack-name obs-state >/dev/null 2>&1 || LOGINS=obs-phaseU1

(cd services/web && npm ci --no-audit --no-fund && npm run build)
cat > services/web/dist/config.json <<JSON
{ "region": "${AWS_DEFAULT_REGION}", "userPoolId": "$(out "$LOGINS" UserPoolId)",
  "clientId": "$(out "$LOGINS" AppClientId)", "apiBase": "" }
JSON

# Hashed assets can be cached for a year; the page and its settings never.
aws s3 sync services/web/dist/assets "s3://${BUCKET}/assets" --delete --cache-control "public,max-age=31536000,immutable"
aws s3 sync services/web/dist "s3://${BUCKET}" --delete --exclude "assets/*" --cache-control "no-cache"
aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/index.html" "/config.json" "/" >/dev/null
echo "Leasyd web app: ${URL}"
