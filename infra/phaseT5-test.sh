#!/usr/bin/env bash
# Phase T5 tests: tenant operations, end to end on AWS.
#
#   infra/phaseT5-test.sh            full run, including waiting for deletion to finish (~25-40 min)
#   QUICK=1 infra/phaseT5-test.sh    stop once the first purge pass is checked
#
# Two throwaway tenants, A and A-2 (A's id is a prefix of A-2's, the risky
# case for a prefix delete). Checks:
#   - create: streams, a working key, status
#   - rotate: the new key works; the old one keeps working during its grace
#     period, then is refused
#   - usage: after compaction, records per signal exactly as sent, bytes > 0
#   - delete: keys refused, streams gone, every object and index entry of A
#     purged, lookups find nothing, A-2 untouched, usage records kept, and
#     (full run) the tenant reaches "deleted" after a clean second pass
set -uo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
HERE="$(cd "$(dirname "$0")" && pwd)"
A="t5-$(date -u +%H%M%S)"; B="${A}-2"
NL=100; NS=50; NM=20   # logs, spans, metric points sent to A
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
info() { echo "INFO  $*"; }
trap '"$HERE/tenant.sh" delete "$A" >/dev/null 2>&1; "$HERE/tenant.sh" delete "$B" >/dev/null 2>&1' EXIT

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="obs-data-${ACCOUNT}-${AWS_DEFAULT_REGION}"
PAYLOAD_FMT=()
[[ "$(aws --version 2>&1)" == aws-cli/1.* ]] || PAYLOAD_FMT=(--cli-binary-format raw-in-base64-out)
invoke() {  # invoke <function> <payload-json> -> sets RESULT
  local out rc err; out="$(mktemp)"
  err="$(aws lambda invoke --function-name "$1" "${PAYLOAD_FMT[@]}" --cli-read-timeout 900 \
    --payload "$2" --query FunctionError --output text "$out")"; rc=$?
  RESULT="$(cat "$out" 2>/dev/null)"; rm -f "$out"
  (( rc == 0 )) && [[ "$err" == None || -z "$err" ]]
}
py() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); print($1)" "$RESULT"; }
admin() { invoke obs-tenant-admin "$1"; }
ENDPOINT="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)"
post() {  # post <key> <signal> <body> -> prints HTTP code
  curl -s -o /dev/null -w '%{http_code}' -X POST "${ENDPOINT}/v1/$2" -H "x-api-key: $1" \
    -H 'Content-Type: application/json' --data-binary "$3"
}
EMPTY='{"resourceLogs":[]}'
wait_active() {  # a new key flickers while API Gateway propagates it: wait for 5 successes in a row
  local ok=0 code
  for _ in $(seq 60); do
    code="$(post "$1" logs "$EMPTY")"
    if [[ "$code" == 200 ]]; then (( ++ok >= 5 )) && return 0; else ok=0; fi
    sleep 3
  done
  return 1
}
wait_refused() {  # prints seconds until 5 refusals in a row, or fails after 3 minutes
  local t0 bad=0 code; t0=$(date +%s)
  while (( $(date +%s) - t0 < 180 )); do
    code="$(post "$1" logs "$EMPTY")"
    if [[ "$code" == 401 || "$code" == 403 ]]; then (( ++bad >= 5 )) && { echo $(( $(date +%s) - t0 )); return 0; }
    else bad=0; fi
    sleep 2
  done
  return 1
}
streams() {  # how many of the tenant's 3 streams exist
  local n=0 sig
  for sig in logs traces metrics; do
    aws firehose describe-delivery-stream --delivery-stream-name "obs-t-$1-${sig}" >/dev/null 2>&1 && n=$((n + 1))
  done
  echo $n
}
objects() { aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$1" --query 'length(not_null(Contents, `[]`))' --output text; }
index_items() {  # items whose key starts "<tenant>#" or "_lease#<tenant>#"
  aws dynamodb scan --table-name obs-index --select COUNT \
    --filter-expression 'begins_with(pk, :a) OR begins_with(pk, :b)' \
    --expression-attribute-values "{\":a\":{\"S\":\"$1#\"},\":b\":{\"S\":\"_lease#$1#\"}}" --query Count --output text
}

# ---- 1. Create ----
KEY1="$("$HERE/tenant.sh" create "$A" 2>/dev/null)" && KEYB="$("$HERE/tenant.sh" create "$B" 2>/dev/null)" \
  || { fail "could not create tenants"; exit 1; }
admin "{\"action\":\"status\",\"tenant\":\"${A}\"}"
[[ "$(py "d['status'], d['plan'], [k['status'] for k in d['keys']]")" == "('active', 'standard', ['active'])" ]] \
  && pass "created ${A}: active, standard plan, one key" || fail "status after create: $RESULT"
n=$(streams "$A")
[[ "$n" == 3 ]] && pass "3 ingest streams for ${A}" || fail "${n} streams for ${A}"
wait_active "$KEY1" && wait_active "$KEYB" && pass "keys active" || { fail "key not active"; exit 1; }

# ---- 2. Rotate with a 3-minute grace ----
KEY2="$("$HERE/tenant.sh" rotate "$A" 0.05 2>/dev/null)" || { fail "rotate"; exit 1; }
rotated=$(date +%s)
[[ "$KEY2" != "$KEY1" ]] && wait_active "$KEY2" && pass "rotated: new key works" || fail "new key not active"
[[ "$(post "$KEY1" logs "$EMPTY")" == 200 ]] && pass "old key still works during its grace period" \
  || fail "old key refused during grace"

# ---- 3. Send data with the new key, compact it, check usage ----
NOW_NS="$(date +%s)000000000"
BODIES="$(python3 - "$NOW_NS" "$NL" "$NS" "$NM" <<'EOF'
import json, secrets, sys
now, nl, ns, nm = map(int, sys.argv[1:])
res = {"attributes": [{"key": "service.name", "value": {"stringValue": "t5-svc"}}]}
print(json.dumps({"resourceLogs": [{"resource": res, "scopeLogs": [{"logRecords": [
    {"timeUnixNano": str(now + i), "body": {"stringValue": f"m{i}"}} for i in range(nl)]}]}]}))
print(json.dumps({"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": [
    {"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "name": "op",
     "startTimeUnixNano": str(now + i), "endTimeUnixNano": str(now + i + 100)} for i in range(ns)]}]}]}))
print(json.dumps({"resourceMetrics": [{"resource": res, "scopeMetrics": [{"metrics": [
    {"name": "g", "gauge": {"dataPoints": [{"timeUnixNano": str(now + i), "asDouble": i} for i in range(nm)]}}]}]}]}))
EOF
)"
i=0; declare -A WANT=([logs]=$NL [traces]=$NS [metrics]=$NM)
for sig in logs traces metrics; do
  i=$((i + 1)); body="$(sed -n "${i}p" <<<"$BODIES")"
  [[ "$(post "$KEY2" "$sig" "$body")" == 200 ]] || fail "send ${sig} to ${A}"
  [[ "$(post "$KEYB" "$sig" "$body")" == 200 ]] || fail "send ${sig} to ${B}"
done
info "sent ${NL} logs, ${NS} spans, ${NM} metric points to each tenant; waiting for Firehose (~60 s)"
sleep 75
for sig in logs traces metrics; do
  for hour_prefix in $(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "_incoming/tenant=${A}/${sig}/" \
      --query 'Contents[].Key' --output text | tr '\t' '\n' | sed -E 's#^(.*/hour=[0-9]+)/.*#\1#' | sort -u); do
    dt="$(sed -E 's#.*/dt=([0-9-]+)/.*#\1#' <<<"$hour_prefix/")"; hr="$(sed -E 's#.*/hour=([0-9]+)$#\1#' <<<"$hour_prefix")"
    ev="\"tenant\":\"${A}\",\"signal\":\"${sig}\",\"dt\":\"${dt}\",\"hour\":\"${hr}\""
    invoke obs-compaction-dispatcher "{\"plan_only\":{${ev}}}" || fail "planning ${sig}"
    for b in $(py "' '.join(d['planned'])"); do
      invoke obs-compaction-worker "{${ev},\"batch_id\":\"${b}\"}" || fail "compacting ${sig}: $RESULT"
    done
  done
done
admin "{\"action\":\"usage\",\"tenant\":\"${A}\"}"
for sig in logs traces metrics; do
  got="$(py "d['totals'].get('${sig}', {}).get('records', 0), d['totals'].get('${sig}', {}).get('raw_bytes', 0) > 0, d['totals'].get('${sig}', {}).get('stored_bytes', 0) > 0")"
  [[ "$got" == "(${WANT[$sig]}, True, True)" ]] && pass "usage ${sig}: ${WANT[$sig]} records, raw and stored bytes counted" \
    || fail "usage ${sig}: ${got} (want ${WANT[$sig]} records, bytes > 0)"
done

# ---- 4. The old key is refused once its grace ends ----
wait_s=$(( rotated + 3 * 60 - $(date +%s) )); (( wait_s > 0 )) && { info "waiting ${wait_s}s for the old key's grace to end"; sleep "$wait_s"; }
if t=$(wait_refused "$KEY1"); then pass "old key refused ${t}s after its grace ended"; else fail "old key still accepted after grace"; fi
[[ "$(post "$KEY2" logs "$EMPTY")" == 200 ]] && pass "new key still works" || fail "new key refused"

# ---- 5. Delete A ----
admin "{\"action\":\"delete\",\"tenant\":\"${A}\"}"
[[ "$(py "d['status']")" == deleting ]] && pass "delete accepted: ${A} is deleting" || fail "delete: $RESULT"
if t=$(wait_refused "$KEY2"); then pass "key refused ${t}s after delete"; else fail "key still accepted after delete"; fi
info "waiting for the first purge pass"
for _ in $(seq 60); do
  admin "{\"action\":\"status\",\"tenant\":\"${A}\"}"; [[ "$(py "d.get('passes', 0)")" -ge 1 ]] && break; sleep 5
done
n=$(streams "$A")
[[ "$n" == 0 ]] && pass "${A}'s streams deleted" || fail "${n} streams left for ${A}"
left="$(objects "_incoming/tenant=${A}/") $(objects "data/tenant=${A}/") $(index_items "$A")"
[[ "$left" == "0 0 0" ]] && pass "first pass purged every raw file, Parquet file and index entry of ${A}" \
  || fail "left after first pass (raw, parquet, index): ${left}"
Q="\"start\":\"$(date -u -d '-2 hour' +%FT%TZ)\",\"end\":\"$(date -u -d '+1 hour' +%FT%TZ)\""
invoke obs-index-lookup "{\"tenant\":\"${A}\",\"signal\":\"logs\",${Q}}"
[[ "$(py "len(d['files'])")" == 0 ]] && pass "lookups find nothing for ${A}" || fail "lookup still finds files for ${A}"
b_left="$(objects "_incoming/tenant=${B}/") $(index_items "$B") $(streams "$B")"
read -r b_raw b_idx b_streams <<<"$b_left"
(( b_raw >= 3 && b_idx >= 3 && b_streams == 3 )) \
  && pass "${B} untouched: ${b_raw} raw files, ${b_idx} index entries, 3 streams" \
  || fail "${B} affected (raw files, index entries, streams): ${b_left}"
[[ "$(post "$KEYB" logs "$EMPTY")" == 200 ]] && pass "${B}'s key still works" || fail "${B}'s key refused"
admin "{\"action\":\"usage\",\"tenant\":\"${A}\"}"
[[ "$(py "d['totals'].get('logs', {}).get('records', 0)")" == "$NL" ]] && pass "usage records kept after deletion" \
  || fail "usage records gone"

if [[ -n "${QUICK:-}" ]]; then info "QUICK: not waiting for the deletion to finish"; exit $FAILED; fi
info "waiting for the second pass and the sweep to mark ${A} deleted (up to 45 min)"
t0=$(date +%s)
while (( $(date +%s) - t0 < 2700 )); do
  admin "{\"action\":\"status\",\"tenant\":\"${A}\"}"; [[ "$(py "d['status']")" == deleted ]] && break; sleep 60
done
[[ "$(py "d['status'], d.get('passes', 0) >= 2, d.get('last_pass_deleted')")" == "('deleted', True, 0)" ]] \
  && pass "${A} deleted after $(( ($(date +%s) - t0) / 60 )) more min: second pass found nothing" \
  || fail "not deleted: $(py "d['status'], d.get('passes'), d.get('last_pass_deleted')")"

exit $FAILED
