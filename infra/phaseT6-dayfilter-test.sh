#!/usr/bin/env bash
# T6 day-filter test, end to end on AWS.
#
#   infra/phaseT6-dayfilter-test.sh
#
# Sends logs with known trace ids for a throwaway tenant, compacts them, seals
# that day's filter (obs-day-sealer's test hook), then checks:
#   - a known trace id is found, with the day filter consulted
#   - an unknown trace id rules the whole day out without reading any file blooms
#   - late data makes the day untrusted (lookups fall back and still find it)
#     until it is re-sealed, after which the day filter is used again
set -uo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
HERE="$(cd "$(dirname "$0")" && pwd)"
T="t6d-$(date -u +%H%M%S)"
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
py() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(($1))" "$RESULT"; }
ENDPOINT="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)"
post() { curl -s -o /dev/null -w '%{http_code}' -X POST "${ENDPOINT}/v1/logs" -H "x-api-key: ${KEY}" \
  -H 'Content-Type: application/json' --data-binary "$1"; }

KEY="$("$HERE/tenant.sh" create "$T" 2>/dev/null)" || { fail "could not create tenant"; exit 1; }
ok=0
for _ in $(seq 60); do
  if [[ "$(post '{"resourceLogs":[]}')" == 200 ]]; then (( ++ok >= 5 )) && break; else ok=0; fi
  sleep 3
done
(( ok >= 5 )) || { fail "key not active"; exit 1; }

batch() {  # batch <first> <count> -> OTLP JSON with trace ids <first>..<first+count-1>
  python3 - "$1" "$2" "$(date +%s)000000000" <<'EOF'
import json, sys
first, n, now = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
recs = [{"timeUnixNano": str(now + i), "body": {"stringValue": "m"}, "traceId": f"{first + i:032x}"} for i in range(n)]
print(json.dumps({"resourceLogs": [{"resource": {"attributes": [
    {"key": "service.name", "value": {"stringValue": "t6-day"}}]}, "scopeLogs": [{"logRecords": recs}]}]}))
EOF
}
compact_all() {  # compact every raw hour the tenant has
  local hour_prefix dt hr ev
  for hour_prefix in $(aws s3api list-objects-v2 --bucket "obs-data-$(aws sts get-caller-identity --query Account --output text)-${AWS_DEFAULT_REGION}" \
      --prefix "_incoming/tenant=${T}/logs/" --query 'Contents[].Key' --output text | tr '\t' '\n' \
      | sed -E 's#^(.*/hour=[0-9]+)/.*#\1#' | sort -u); do
    dt="$(sed -E 's#.*/dt=([0-9-]+)/.*#\1#' <<<"$hour_prefix/")"; hr="${hour_prefix##*hour=}"
    ev="\"tenant\":\"${T}\",\"signal\":\"logs\",\"dt\":\"${dt}\",\"hour\":\"${hr}\""
    invoke obs-compaction-dispatcher "{\"plan_only\":{${ev}}}" || fail "planning: $RESULT"
    for b in $(py "' '.join(d['planned'])"); do invoke obs-compaction-worker "{${ev},\"batch_id\":\"${b}\"}" || fail "compacting: $RESULT"; done
  done
}
DAY="$(date -u +%F)"
Q="\"tenant\":\"${T}\",\"signal\":\"logs\",\"start\":\"${DAY}T00:00:00Z\",\"end\":\"${DAY}T23:59:59Z\""
lookup() { invoke obs-index-lookup "{${Q},\"match\":{\"trace_id\":\"$1\"}}"; }
seal() { invoke obs-day-sealer "{\"only\":{\"tenant\":\"${T}\",\"signal\":\"logs\",\"dt\":\"${DAY}\"}}"; }

# ---- 1. Two batches (two services' worth of IDs), compact, seal ----
[[ "$(post "$(batch 1000 400)")" == 200 && "$(post "$(batch 5000 400)")" == 200 ]] && pass "sent 800 records with known trace ids" \
  || { fail "send"; exit 1; }
info "waiting for Firehose (~60 s)"; sleep 70
compact_all
seal && [[ "$(py "d['sealed']")" == "['${T}/logs/${DAY}']" ]] && pass "day ${DAY} sealed" || fail "seal: $RESULT"

# ---- 2. Lookups use the day filter ----
lookup "$(printf '%032x' 1234)"
[[ "$(py "len(d['files']), d['stats']['days_checked']")" == "(1, 1)" ]] && pass "known trace id found, day filter consulted" \
  || fail "known id: $(py "len(d['files']), d['stats']")"
lookup "$(printf 'f%.0s' {1..32})"
[[ "$(py "len(d['files']), d['stats']['days_ruled_out'], d['stats']['bloom_fetches']")" == "(0, 1, 0)" ]] \
  && pass "unknown trace id: whole day ruled out, no per-file blooms read ($(py "d['stats']['ms']") ms)" \
  || fail "unknown id: $(py "d['stats']")"

# ---- 3. Late data: day untrusted until re-sealed ----
[[ "$(post "$(batch 9000 10)")" == 200 ]] || fail "send late batch"
info "waiting for Firehose (~60 s)"; sleep 70
compact_all
lookup "$(printf '%032x' 9005)"
[[ "$(py "len(d['files']), d['stats']['days_checked']")" == "(1, 0)" ]] \
  && pass "late record found via per-file blooms while the day filter is out of date" \
  || fail "late id before reseal: $(py "len(d['files']), d['stats']")"
seal
lookup "$(printf '%032x' 9005)"
[[ "$(py "len(d['files']), d['stats']['days_checked']")" == "(1, 1)" ]] \
  && pass "after re-sealing, the late record is found with the day filter" \
  || fail "late id after reseal: $(py "len(d['files']), d['stats']")"

exit $FAILED
