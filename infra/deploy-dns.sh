#!/usr/bin/env bash
# The product's names (obs-dns): DNS zones for ingest.<domain> and app.<domain>, then, once the
# domain's DNS delegates them, the certificate for both. The domain itself (website, email) stays
# with its current DNS provider.
#   infra/deploy-dns.sh leasyd.com      first time: creates the zones and prints the NS records to add
#   infra/deploy-dns.sh                 later runs: reuses the domain of the existing zones
# Safe to re-run: it issues the certificate as soon as the NS records are visible.
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

# Does public DNS delegate <name> to its zone yet? (Asks as the certificate authority will.)
normalize() { tr ' ' '\n' | tr 'A-Z' 'a-z' | sed 's/\.$//' | grep -v '^$' | sort | xargs; }
delegated() {  # delegated <name> <zone name servers>
  local public
  public="$(curl -s "https://dns.google/resolve?name=$1&type=NS" | python3 -c '
import json, sys
print(" ".join(a["data"] for a in json.load(sys.stdin).get("Answer", []) if a.get("type") == 2))' | normalize)"
  [[ "$public" == "$(echo "$2" | normalize)" ]]
}
INGEST_NS="$(out IngestNameServers)"; APP_NS="$(out AppNameServers)"

if [[ -z "$CERT" ]] && ! { delegated "ingest.${DOMAIN}" "$INGEST_NS" && delegated "app.${DOMAIN}" "$APP_NS"; }; then
  cat >&2 <<EOF

The zones are ready. Where ${DOMAIN}'s DNS is managed today (e.g. Vercel), add these 8 records.
Leave every other record as it is: the website and email are not affected.

  Type  Name     Value
$(for ns in $INGEST_NS; do printf '  NS    %-8s %s\n' ingest "$ns"; done)
$(for ns in $APP_NS; do printf '  NS    %-8s %s\n' app "$ns"; done)

They can take from minutes to a few hours to show. Then run infra/deploy-dns.sh again to issue
the certificate. Delegated so far: ingest $(delegated "ingest.${DOMAIN}" "$INGEST_NS" && echo yes || echo no), app $(delegated "app.${DOMAIN}" "$APP_NS" && echo yes || echo no).
EOF
  exit 0
fi

if [[ -z "$CERT" ]]; then
  echo "ingest.${DOMAIN} and app.${DOMAIN} are delegated; issuing the certificate (usually a few minutes)" >&2
  aws cloudformation deploy --stack-name obs-dns --template-file infra/dns.yaml \
    --tags project=obs phase=dns --parameter-overrides "DomainName=${DOMAIN}" IssueCertificate=true
fi
echo "certificate ready. infra/deploy-phaseT2.sh and infra/deploy-phaseW1.sh (or infra/up.sh) now" >&2
echo "serve https://ingest.${DOMAIN} and https://app.${DOMAIN}." >&2
