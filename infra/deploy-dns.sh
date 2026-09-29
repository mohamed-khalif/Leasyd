#!/usr/bin/env bash
# The product's domain (obs-dns): a DNS zone, then, once the registrar points the domain at it, the
# certificate for ingest.<domain> and app.<domain>.
#   infra/deploy-dns.sh leasyd.com      first time: creates the zone and prints its name servers
#   infra/deploy-dns.sh                 later runs: reuses the domain of the existing zone
# Safe to re-run: it issues the certificate as soon as the name servers are in place.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
out() { aws cloudformation describe-stacks --stack-name obs-dns \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true; }

DOMAIN="${1:-$(out DomainName)}"
[[ -n "$DOMAIN" && "$DOMAIN" != None ]] || { echo "usage: infra/deploy-dns.sh <domain>" >&2; exit 1; }
CERT="$(out CertificateArn)"; [[ "$CERT" == None ]] && CERT=""

if [[ -z "$CERT" ]]; then
  aws cloudformation deploy --stack-name obs-dns --template-file infra/dns.yaml \
    --tags project=obs phase=dns --parameter-overrides "DomainName=${DOMAIN}" IssueCertificate=false
fi

# Is the domain delegated to this zone yet? (Asks public DNS, as the certificate authority will.)
ZONE_NS="$(out NameServers)"
PUBLIC_NS="$(curl -s "https://dns.google/resolve?name=${DOMAIN}&type=NS" | python3 -c '
import json, sys
print(" ".join(sorted(a["data"].rstrip(".").lower() for a in json.load(sys.stdin).get("Answer", []) if a.get("type") == 2)))')"
WANT="$(echo "$ZONE_NS" | tr " " "\n" | tr A-Z a-z | sed 's/\.$//' | sort | xargs)"

if [[ -z "$CERT" && "$PUBLIC_NS" != "$WANT" ]]; then
  cat >&2 <<EOF

The ${DOMAIN} zone is ready. At your domain's registrar, replace its name servers with:

$(echo "$ZONE_NS" | tr " " "\n" | sed 's/^/    /')

(now: ${PUBLIC_NS:-none found}). The change can take from minutes to a few hours to show.
Then run infra/deploy-dns.sh again (or infra/up.sh) to issue the certificate.
EOF
  exit 0
fi

if [[ -z "$CERT" ]]; then
  echo "name servers point at the zone; issuing the certificate (usually a few minutes)" >&2
  aws cloudformation deploy --stack-name obs-dns --template-file infra/dns.yaml \
    --tags project=obs phase=dns --parameter-overrides "DomainName=${DOMAIN}" IssueCertificate=true
fi
echo "domain ready: https://ingest.${DOMAIN} and https://app.${DOMAIN} (after infra/up.sh)" >&2
