#!/usr/bin/env bash
# Deletes the load test's accounts (t6-NNN, made 27 Sep 2026: their API keys, ingest streams and data)
# and removes revoked API Gateway keys (disabled, used by nothing). Keeps every other account and
# the key pool (pool-*: customers' keys borrow these). Shows what it will delete and asks first.
#
#   AWS_DEFAULT_REGION=us-east-1 bash infra/cleanup-test-accounts.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:=us-east-1}"; export AWS_DEFAULT_REGION

mapfile -t accounts < <(infra/tenant.sh list 2>/dev/null | awk '$2 == "active" && $1 ~ /^t6-[0-9][0-9][0-9]($|-)/ {print $1}')
mapfile -t revoked < <(aws apigateway get-api-keys --limit 500 \
  --query 'items[?enabled==`false` && !starts_with(name, `pool-`)].[id,name]' --output text)
echo "Load-test accounts to delete (${#accounts[@]}): ${accounts[*]:0:5} ..."
echo "Revoked gateway keys to remove (${#revoked[@]}):"; printf '  %s\n' "${revoked[@]}"
echo "Kept: every other account (e.g. $(infra/tenant.sh list 2>/dev/null | awk '$2 == "active" && $1 !~ /^t6-/ {print $1}' | tr '\n' ' '))and the pool-* keys."
read -r -p "Delete them? [y/N] " ok
[[ "$ok" == y || "$ok" == Y ]] || { echo "nothing deleted"; exit 0; }

for t in "${accounts[@]}"; do infra/tenant.sh delete "$t" >/dev/null && echo "deleting account $t"; done
for line in "${revoked[@]}"; do
  [[ -n "$line" ]] || continue
  aws apigateway delete-api-key --api-key "${line%%[[:space:]]*}" && echo "removed key ${line#*[[:space:]]}"
done
echo "Done. Each account's data is purged in passes every 20 minutes until nothing is left (usually within hours)."
