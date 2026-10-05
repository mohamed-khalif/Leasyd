#!/usr/bin/env bash
# The public website (obs-site): uploads site/ and serves it at www.<domain>.
#   infra/deploy-site.sh leasyd.com   first time: creates the zone, bucket and CloudFront, uploads the
#                                     pages and prints the DNS records to add where the domain lives
#   infra/deploy-site.sh              later runs: same domain; re-uploads the pages
# Until www.<domain> is delegated it is served at a *.cloudfront.net address (printed at the end);
# once the NS records are visible, a run issues the certificate and switches www.<domain> on.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
out() { aws cloudformation describe-stacks --stack-name obs-site \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true; }

DOMAIN="${1:-$(out DomainName)}"
[[ -n "$DOMAIN" && "$DOMAIN" != None ]] || { echo "usage: infra/deploy-site.sh <domain>" >&2; exit 1; }
CERT="$(out CertificateArn)"; [[ "$CERT" == None ]] && CERT=""

deploy() {
  aws cloudformation deploy --stack-name obs-site --template-file infra/site.yaml \
    --tags project=obs phase=site --no-fail-on-empty-changeset \
    --parameter-overrides "DomainName=${DOMAIN}" "IssueCertificate=$1"
}
normalize() { tr ' ' '\n' | tr 'A-Z' 'a-z' | sed 's/\.$//' | grep -v '^$' | sort | xargs; }
delegated() {   # does public DNS hand www.<domain> to our zone yet?
  local public
  public="$(curl -s "https://dns.google/resolve?name=www.${DOMAIN}&type=NS" | python3 -c '
import json, sys
print(" ".join(a["data"] for a in json.load(sys.stdin).get("Answer", []) if a.get("type") == 2))' | normalize)"
  [[ -n "$public" && "$public" == "$(out NameServers | normalize)" ]]
}

if [[ -n "$CERT" ]]; then
  deploy true
else
  deploy false
  if delegated; then
    echo "www.${DOMAIN} is delegated: issuing its certificate (a few minutes)..."
    deploy true
  fi
fi

# Pages, styles and scripts are always revalidated (so a deploy shows at once); images cached an hour.
BUCKET="$(out BucketName)"
aws s3 sync site/ "s3://${BUCKET}/" --delete --exclude "*" --include "img/*" --cache-control "public,max-age=3600"
aws s3 sync site/ "s3://${BUCKET}/" --delete --exclude "*" --include "*.html" --include "*.css" --include "*.js" \
  --cache-control "no-cache"
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" >/dev/null

if [[ -z "$(out CertificateArn)" || "$(out CertificateArn)" == None ]]; then
  cat <<EOM

The website is up at $(out SiteUrl) (a temporary address).
To serve it at https://www.${DOMAIN}, in ${DOMAIN}'s DNS (GoDaddy):
  1. Delete the existing "www" record (a CNAME), if there is one.
  2. Add these 4 records:
$(for ns in $(out NameServers); do printf '       Type NS   Name www   Value %s\n' "${ns%.}"; done)
  3. Forward ${DOMAIN} itself to https://www.${DOMAIN} (GoDaddy: Domain > Forwarding, permanent 301).
     Email (MX records) is not affected.
Then run infra/deploy-site.sh again: it issues the certificate and switches www.${DOMAIN} on.
EOM
else
  echo "The website is live at https://www.${DOMAIN}"
fi
