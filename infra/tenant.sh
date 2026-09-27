#!/usr/bin/env bash
# Tenant API keys for the ingest endpoint.
#
#   infra/tenant.sh create <tenant> [standard|test-tiny]   new API key for a tenant (printed once)
#   infra/tenant.sh revoke <tenant>                        disable all of a tenant's keys
#   infra/tenant.sh list                                   tenants and key status
#
# The key goes to API Gateway (for usage-plan limits); only its SHA-256 goes
# into obs-tenants, which the authorizer uses to map key -> tenant.
set -euo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
TABLE=obs-tenants
out() { aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

check_tenant() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$ ]] || { echo "tenant id: 3-40 chars of a-z, 0-9, '-'" >&2; exit 2; }
}

case "${1:-}" in
  create)
    tenant="${2:?tenant}"; plan="${3:-standard}"; check_tenant "$tenant"
    case "$plan" in standard) plan_id="$(out StandardPlanId)" ;; test-tiny) plan_id="$(out TestTinyPlanId)" ;;
      *) echo "plan: standard or test-tiny" >&2; exit 2 ;; esac
    key="obs_$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
    hash="$(printf '%s' "$key" | sha256sum | cut -d' ' -f1)"
    key_id="$(aws apigateway create-api-key --name "${tenant}-$(date -u +%Y%m%dT%H%M%S)" \
      --value "$key" --enabled --tags "tenant=${tenant},project=obs" --query id --output text)"
    aws apigateway create-usage-plan-key --usage-plan-id "$plan_id" --key-id "$key_id" --key-type API_KEY >/dev/null
    aws dynamodb put-item --table-name "$TABLE" --condition-expression 'attribute_not_exists(pk)' --item \
      "{\"pk\":{\"S\":\"key#${hash}\"},\"tenant\":{\"S\":\"${tenant}\"},\"status\":{\"S\":\"active\"},
        \"api_key_id\":{\"S\":\"${key_id}\"},\"plan\":{\"S\":\"${plan}\"},\"created_at\":{\"S\":\"$(date -u +%FT%TZ)\"}}"
    echo "tenant:   ${tenant} (${plan} plan)" >&2
    echo "endpoint: $(out IngestEndpoint)" >&2
    echo "api key (shown once; store it securely):" >&2
    echo "$key"
    ;;
  revoke)
    tenant="${2:?tenant}"; check_tenant "$tenant"
    rows="$(aws dynamodb scan --table-name "$TABLE" --filter-expression 'tenant = :t AND #s = :a' \
      --expression-attribute-names '{"#s":"status"}' \
      --expression-attribute-values "{\":t\":{\"S\":\"${tenant}\"},\":a\":{\"S\":\"active\"}}" \
      --query 'Items[].[pk.S, api_key_id.S]' --output text)"
    [[ -z "$rows" ]] && { echo "no active keys for ${tenant}"; exit 0; }
    while read -r pk key_id; do
      # Disabling the API Gateway key refuses it immediately; the status change
      # stops the authorizer accepting it once its cached answer expires.
      aws apigateway update-api-key --api-key "$key_id" --patch-operations op=replace,path=/enabled,value=false >/dev/null
      aws dynamodb update-item --table-name "$TABLE" --key "{\"pk\":{\"S\":\"${pk}\"}}" \
        --update-expression 'SET #s = :r, revoked_at = :now' --expression-attribute-names '{"#s":"status"}' \
        --expression-attribute-values "{\":r\":{\"S\":\"revoked\"},\":now\":{\"S\":\"$(date -u +%FT%TZ)\"}}"
      echo "revoked key ${key_id} of ${tenant}"
    done <<<"$rows"
    ;;
  list)
    aws dynamodb scan --table-name "$TABLE" --query 'Items[].[tenant.S, status.S, plan.S, api_key_id.S, created_at.S]' \
      --output text | sort
    ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
