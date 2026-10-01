#!/usr/bin/env bash
# Tenant operations, through the obs-tenant-admin Lambda (Phase T5).
#
#   infra/tenant.sh create <tenant> [standard|free|test-tiny]  streams + first API key (key printed once, on stdout)
#   infra/tenant.sh rotate <tenant> [grace-hours]         new key (stdout); old keys work for grace-hours (default 24)
#   infra/tenant.sh read-key <tenant>                     an extra key that may only query (POST /v1/query), shown once
#   infra/tenant.sh invite-user <tenant> <email>          a person who signs in to the product; emailed a temporary password
#   infra/tenant.sh remove-user <tenant> <email>          sign them out everywhere and delete the login
#   infra/tenant.sh users <tenant>                        the tenant's users
#   infra/tenant.sh revoke <tenant> [key-id]              refuse one key, or all of the tenant's keys
#   infra/tenant.sh delete <tenant>                       refuse all keys, delete streams, purge all data and index
#   infra/tenant.sh set-cap <tenant> <gb-a-day|0|plan>   the most data a day ingest accepts (0: no cap; plan: the plan's)
#   infra/tenant.sh tune <tenant> <buffer-seconds>        Firehose buffer before a file is written (default 30;
#                                                         shorter for high-volume tenants: fresher, more files)
#   infra/tenant.sh status <tenant>                       status, plan, keys (ids and states only)
#   infra/tenant.sh usage <tenant> [start] [end]          records and bytes per day and signal (YYYY-MM-DD)
#   infra/tenant.sh list                                  all tenants
#   infra/tenant.sh restore                               after infra/up.sh recreates the API: live keys back in
#                                                         their usage plans, streams for every active tenant
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
    echo "api key (shown once; store it securely). It becomes fully active within ~10 minutes (refused on some requests until then):" >&2
    field "['api_key']"
    ;;
  rotate)
    admin "{\"action\":\"rotate\",\"tenant\":\"${tenant}\",\"grace_hours\":${3:-24}}"
    echo "new key id $(field "['key_id']"); old keys $(field "['old_key_ids']") work until $(field "['old_keys_expire_at']")" >&2
    echo "new api key (shown once):" >&2
    field "['api_key']"
    ;;
  read-key)
    admin "{\"action\":\"read-key\",\"tenant\":\"${tenant}\"}"
    echo "read key id $(field "['key_id']") (may only query; fully active within ~10 minutes). api key (shown once):" >&2
    field "['api_key']"
    ;;
  invite-user)
    admin "{\"action\":\"invite-user\",\"tenant\":\"${tenant}\",\"email\":\"${3:?email}\"}"; pretty
    ;;
  remove-user)
    admin "{\"action\":\"remove-user\",\"tenant\":\"${tenant}\",\"email\":\"${3:?email}\"}"; pretty
    ;;
  users)
    admin "{\"action\":\"users\",\"tenant\":\"${tenant}\"}"; pretty
    ;;
  revoke)
    if [[ -n "${3:-}" ]]; then admin "{\"action\":\"revoke\",\"tenant\":\"${tenant}\",\"key_id\":\"$3\"}"
    else admin "{\"action\":\"revoke\",\"tenant\":\"${tenant}\"}"; fi
    echo "revoked $(field "['revoked']") (refused everywhere within about a minute)"
    ;;
  delete)
    admin "{\"action\":\"delete\",\"tenant\":\"${tenant}\"}"; pretty
    ;;
  set-cap)
    v="${3:?GB a day, 0 for no cap, or plan}"; [[ "$v" == plan ]] && v=null
    admin "{\"action\":\"set-cap\",\"tenant\":\"${tenant}\",\"daily_gb\":${v}}"; pretty
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
    print("%-42s %-9s %-10s %s" % (t["tenant"], t["status"], t.get("plan") or "", t.get("created_at") or ""))' "$RESULT"
    ;;
  restore)
    admin '{"action":"restore"}'
    python3 -c 'import json,sys; r=json.loads(sys.argv[1])
print("restored %d tenants; %d keys put back in their usage plans" % (len(r["tenants"]), len(r["keys_added_to_plans"])))' "$RESULT" >&2
    ;;
  *) sed -n '2,19p' "$0"; exit 2 ;;
esac
