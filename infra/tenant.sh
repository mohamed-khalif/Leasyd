#!/usr/bin/env bash
# Tenant API keys for the ingest endpoint.
#
#   infra/tenant.sh create <tenant> [standard|test-tiny]   ingest streams + a new API key (printed once)
#   infra/tenant.sh provision <tenant>                     create any missing ingest streams
#   infra/tenant.sh revoke <tenant>                        disable all of a tenant's keys
#   infra/tenant.sh list                                   tenants and key status
#
# The key goes to API Gateway (for usage-plan limits); only its SHA-256 goes
# into obs-tenants, which the authorizer uses to map key -> tenant. Each
# tenant gets its own Firehose stream per signal, obs-t-<tenant>-<signal>,
# delivering to _incoming/tenant=<tenant>/<signal>/ (flush every
# BUFFER_SECONDS, default 30, or at 64 MB).
set -euo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
TABLE=obs-tenants
out() { aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="obs-data-${ACCOUNT}-${AWS_DEFAULT_REGION}"
BUFFER_SECONDS="${BUFFER_SECONDS:-30}"

provision() {  # provision <tenant>: one Firehose stream per signal, then wait until all are ACTIVE
  local tenant="$1" role sig name dest
  role="$(out FirehoseRoleArn)"
  for sig in logs traces metrics; do
    name="obs-t-${tenant}-${sig}"
    aws firehose describe-delivery-stream --delivery-stream-name "$name" >/dev/null 2>&1 && continue
    dest="{\"RoleARN\":\"${role}\",\"BucketARN\":\"arn:aws:s3:::${BUCKET}\",
      \"Prefix\":\"_incoming/tenant=${tenant}/${sig}/dt=!{timestamp:yyyy-MM-dd}/hour=!{timestamp:HH}/\",
      \"ErrorOutputPrefix\":\"_incoming/_errors/tenant=${tenant}/${sig}/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/\",
      \"BufferingHints\":{\"SizeInMBs\":64,\"IntervalInSeconds\":${BUFFER_SECONDS}},
      \"CompressionFormat\":\"GZIP\",\"FileExtension\":\".json.gz\"}"
    aws firehose create-delivery-stream --delivery-stream-name "$name" --delivery-stream-type DirectPut \
      --extended-s3-destination-configuration "$dest" \
      --tags "Key=tenant,Value=${tenant}" "Key=project,Value=obs" >/dev/null
    echo "creating stream ${name}" >&2
  done
  for sig in logs traces metrics; do
    name="obs-t-${tenant}-${sig}"
    for _ in $(seq 60); do
      [[ "$(aws firehose describe-delivery-stream --delivery-stream-name "$name" \
        --query DeliveryStreamDescription.DeliveryStreamStatus --output text)" == ACTIVE ]] && break
      sleep 5
    done
  done
  echo "streams ready for ${tenant}" >&2
}

check_tenant() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$ ]] || { echo "tenant id: 3-40 chars of a-z, 0-9, '-'" >&2; exit 2; }
}

case "${1:-}" in
  create)
    tenant="${2:?tenant}"; plan="${3:-standard}"; check_tenant "$tenant"
    case "$plan" in standard) plan_id="$(out StandardPlanId)" ;; test-tiny) plan_id="$(out TestTinyPlanId)" ;;
      *) echo "plan: standard or test-tiny" >&2; exit 2 ;; esac
    provision "$tenant"
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
  provision)
    tenant="${2:?tenant}"; check_tenant "$tenant"; provision "$tenant"
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
  *) sed -n '2,14p' "$0"; exit 2 ;;
esac
