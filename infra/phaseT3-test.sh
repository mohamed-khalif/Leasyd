#!/usr/bin/env bash
# Phase T3 tests: the fast lane, end to end on AWS.
#
#   infra/phaseT3-test.sh
#
# Creates a throwaway tenant, sends a batch of logs through the public
# endpoint, and checks:
#   - freshness: how long until a lookup finds the new records (as a raw file)
#   - the lookup's row count is exactly what was sent, and a trace id lookup finds it
#   - after compacting that hour, the same lookup returns Parquet only, with the
#     same row count (no double count, nothing lost), and no fast-lane leftovers
set -uo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SUFFIX="$(date -u +%H%M%S)"
T="t3-${SUFFIX}"
N=500
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
info() { echo "INFO  $*"; }
trap '"$HERE/tenant.sh" delete "$T" >/dev/null 2>&1' EXIT

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

ENDPOINT="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)"
KEY="$("$HERE/tenant.sh" create "$T" 2>/dev/null)" || { fail "could not create tenant"; exit 1; }
info "tenant ${T}; waiting for its key to become active"
# A new key flickers between 200 and 403 while API Gateway propagates it: wait for 5 in a row.
ok=0
for _ in $(seq 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${ENDPOINT}/v1/logs" -H "x-api-key: ${KEY}" \
    -H 'Content-Type: application/json' --data '{"resourceLogs":[]}')
  if [[ "$code" == 200 ]]; then (( ++ok >= 5 )) && break; else ok=0; fi
  sleep 3
done
(( ok >= 5 )) || { fail "key not active (HTTP ${code})"; exit 1; }

# ---- 1. Send N records in one request ----
TRACE="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
NOW_NS="$(date +%s)000000000"
BODY="$(python3 - "$N" "$TRACE" "$NOW_NS" <<'EOF'
import json, sys
n, trace, now = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
recs = [{"timeUnixNano": str(now + i * 1000), "severityText": "Info", "body": {"stringValue": f"fast-lane {i}"},
         "traceId": trace if i == 7 else ""} for i in range(n)]
print(json.dumps({"resourceLogs": [{"resource": {"attributes": [
    {"key": "service.name", "value": {"stringValue": "t3-fast"}}]}, "scopeLogs": [{"logRecords": recs}]}]}))
EOF
)"
sent=$(date +%s)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${ENDPOINT}/v1/logs" -H "x-api-key: ${KEY}" \
  -H 'Content-Type: application/json' --data "$BODY")
[[ "$code" == 200 ]] && pass "sent ${N} records for ${T}" || { fail "send -> HTTP ${code}"; exit 1; }

START="$(date -u -d '-10 min' +%FT%TZ)"; END="$(date -u -d '+10 min' +%FT%TZ)"
Q="{\"tenant\":\"${T}\",\"services\":[\"t3-fast\"],\"start\":\"${START}\",\"end\":\"${END}\""

# ---- 2. Freshness: poll the lookup until the records are findable ----
found=""
while (( $(date +%s) - sent < 180 )); do
  invoke obs-index-lookup "${Q}}" && [[ "$(py "sum(f['row_count'] for f in d['files'])")" -ge "$N" ]] \
    && { found=$(( $(date +%s) - sent )); break; }
  sleep 3
done
if [[ -n "$found" ]]; then pass "records findable by lookup ${found}s after sending (target: 60s)"
else fail "records not findable after 180s"; exit 1; fi
kinds="$(py "sorted({f['kind'] for f in d['files']})")"; rows="$(py "sum(f['row_count'] for f in d['files'])")"
[[ "$kinds" == "['raw']" && "$rows" == "$N" ]] && pass "lookup returns ${rows} rows, from raw (not yet compacted) files" \
  || fail "before compaction: kinds ${kinds}, rows ${rows} (want ['raw'], ${N})"
RAW_PATH="$(py "d['files'][0]['file_path']")"
DT="$(sed -E 's#.*/dt=([0-9-]+)/.*#\1#' <<<"$RAW_PATH")"; HR="$(sed -E 's#.*/hour=([0-9]+)/.*#\1#' <<<"$RAW_PATH")"
invoke obs-index-lookup "${Q},\"match\":{\"trace_id\":\"${TRACE}\"}}"
[[ "$(py "len(d['files'])")" -ge 1 ]] && pass "trace id lookup finds the raw file" || fail "trace id lookup found nothing"
invoke obs-index-lookup "${Q},\"match\":{\"trace_id\":\"$(python3 -c 'import secrets; print(secrets.token_hex(16))')\"}}"
[[ "$(py "len(d['files'])")" == 0 ]] && pass "an unknown trace id finds nothing (bloom filter on the raw file)" \
  || info "unknown trace id matched $(py "len(d['files'])") file(s) (1% false positives are expected)"

# ---- 3. Compact that arrival hour now, then look again ----
EV="\"tenant\":\"${T}\",\"signal\":\"logs\",\"dt\":\"${DT}\",\"hour\":\"${HR}\""
invoke obs-compaction-dispatcher "{\"plan_only\":{${EV}}}" || { fail "planning: $RESULT"; exit 1; }
for b in $(py "' '.join(d['planned'])"); do
  invoke obs-compaction-worker "{${EV},\"batch_id\":\"${b}\"}" || { fail "compacting ${b}: $RESULT"; exit 1; }
done
invoke obs-index-lookup "${Q}}"
kinds="$(py "sorted({f['kind'] for f in d['files']})")"; rows="$(py "sum(f['row_count'] for f in d['files'])")"
hidden="$(py "d['stats']['hidden_by_handover']")"
[[ "$kinds" == "['parquet']" && "$rows" == "$N" ]] \
  && pass "after compaction: ${rows} rows, all from Parquet (same count: nothing doubled or lost)" \
  || fail "after compaction: kinds ${kinds}, rows ${rows} (want ['parquet'], ${N}); hidden_by_handover=${hidden}"
invoke obs-index-lookup "${Q},\"match\":{\"trace_id\":\"${TRACE}\"}}"
[[ "$(py "[f['kind'] for f in d['files']]")" == "['parquet']" ]] && pass "trace id now found in the Parquet file" \
  || fail "trace id after compaction: $(py "[f['kind'] for f in d['files']]")"
left="$(aws dynamodb query --table-name obs-index --key-condition-expression 'pk = :p' \
  --expression-attribute-values "{\":p\":{\"S\":\"${T}#_raw#logs#${DT}#${HR}\"}}" --query Count --output text)"
(( left == 0 )) && pass "fast-lane records retired" || fail "${left} raw-file records left"

exit $FAILED
