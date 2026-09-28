#!/usr/bin/env bash
# Tenant operations, through the obs-tenant-admin Lambda (Phase T5).
#
#   infra/tenant.sh create <tenant> [standard|test-tiny]  streams + first API key (key printed once, on stdout)
#   infra/tenant.sh rotate <tenant> [grace-hours]         new key (stdout); old keys work for grace-hours (default 24)
#   infra/tenant.sh revoke <tenant> [key-id]              refuse one key, or all of the tenant's keys
#   infra/tenant.sh delete <tenant>                       refuse all keys, delete streams, purge all data and index
#   infra/tenant.sh tune <tenant> <buffer-seconds>        Firehose buffer before a file is written (default 30;
#                                                         shorter for high-volume tenants: fresher, more files)
#   infra/tenant.sh status <tenant>                       status, plan, keys (ids and states only)
#   infra/tenant.sh usage <tenant> [start] [end]          records and bytes per day and signal (YYYY-MM-DD)
#   infra/tenant.sh list                                  all tenants
#
# See services/tenants/admin.py for what each does.
set -euo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
PAYLOAD_FMT=()
[[ "$(aws --version 2>&1)" == aws-cli/1.* ]] || PAYLOAD_FMT=(--cli-binary-format raw-in-base64-out)

admin() {  # admin <payload-json> -> prints the JSON result; exit 1 on error
  local out; out="$(mktemp)"
  local err
  err="$(aws lambda invoke --function-name obs-tenant-admin "${PAYLOAD_FMT[@]}" --cli-read-timeout 900 \
    --payload "$1" --query FunctionError --output text "$out")" || { rm -f "$out"; exit 1; }
  RESULT="$(cat "$out")"; rm -f "$out"
  if [[ "$err" != None && -n "$err" ]] || python3 -c 'import json,sys; sys.exit("error" not in json.loads(sys.argv[1]))' "$RESULT"; then
    echo "$RESULT" >&2; exit 1
  fi
}
field() { python3 -c "import json,sys; print(json.loads(sys.argv[1])$1)" "$RESULT"; }
pretty() { python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1]), indent=2))' "$RESULT"; }

cmd="${1:-}"; tenant="${2:-}"
case "$cmd" in
  create)
    admin "{\"action\":\"create\",\"tenant\":\"${tenant}\",\"plan\":\"${3:-standard}\"}"
    echo "tenant:   ${tenant} ($(field "['plan']") plan), key id $(field "['key_id']")" >&2
    echo "endpoint: $(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
      --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)" >&2
    echo "api key (shown once; store it securely). It becomes active within 1-2 minutes:" >&2
    field "['api_key']"
    ;;
  rotate)
    admin "{\"action\":\"rotate\",\"tenant\":\"${tenant}\",\"grace_hours\":${3:-24}}"
    echo "new key id $(field "['key_id']"); old keys $(field "['old_key_ids']") work until $(field "['old_keys_expire_at']")" >&2
    echo "new api key (shown once):" >&2
    field "['api_key']"
    ;;
  revoke)
    if [[ -n "${3:-}" ]]; then admin "{\"action\":\"revoke\",\"tenant\":\"${tenant}\",\"key_id\":\"$3\"}"
    else admin "{\"action\":\"revoke\",\"tenant\":\"${tenant}\"}"; fi
    echo "revoked $(field "['revoked']") (refused everywhere within about a minute)"
    ;;
  delete)
    admin "{\"action\":\"delete\",\"tenant\":\"${tenant}\"}"; pretty
    ;;
  tune)
    admin "{\"action\":\"tune\",\"tenant\":\"${tenant}\",\"buffer_seconds\":${3:?buffer seconds}}"; pretty
    ;;
  status)
    admin "{\"action\":\"status\",\"tenant\":\"${tenant}\"}"; pretty
    ;;
  usage)
    args="\"tenant\":\"${tenant}\""
    [[ -n "${3:-}" ]] && args+=",\"start\":\"$3\""
    [[ -n "${4:-}" ]] && args+=",\"end\":\"$4\""
    admin "{\"action\":\"usage\",${args}}"; pretty
    ;;
  list)
    admin '{"action":"list"}'
    python3 -c 'import json,sys
for t in json.loads(sys.argv[1])["tenants"]:
    print(f"{t[\"tenant\"]:42} {t[\"status\"]:9} {t[\"plan\"] or \"\":10} {t[\"created_at\"] or \"\"}")' "$RESULT"
    ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
