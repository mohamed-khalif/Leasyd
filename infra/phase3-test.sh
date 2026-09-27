#!/usr/bin/env bash
# Phase 3 tests: index lookups on real AWS.
#
#   infra/phase3-test.sh
#
# Seeds 3 hours x 3 services of logs (2026-09-25 10:00-12:59), each record
# with its own random trace id and request.id, straight into _incoming/,
# compacts them, then checks:
#   - a time-range lookup returns only the overlapping files
#   - a file that starts before the range is included; one that ends before it isn't
#   - a trace id / request.id lookup keeps ~1 of the 9 files, and it's the right one
#   - Athena confirms that file really holds the id (no false negatives)
#   - lookup latency and DynamoDB read cost
# Re-running reuses the seeded data if it's already compacted.
set -uo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="obs-data-${ACCOUNT}-${AWS_DEFAULT_REGION}"
DT=2026-09-25
HOURS=(10 11 12)
SERVICES=(svc-a svc-b svc-c)
PER_FILE="${PER_FILE:-20000}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
SAMPLES="$WORK/samples.tsv"
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }

PAYLOAD_FMT=()
[[ "$(aws --version 2>&1)" == aws-cli/1.* ]] || PAYLOAD_FMT=(--cli-binary-format raw-in-base64-out)
invoke() {  # invoke <function> <payload-json> -> sets RESULT
  local out rc err; out="$(mktemp)"
  err="$(aws lambda invoke --function-name "$1" "${PAYLOAD_FMT[@]}" --cli-read-timeout 900 \
    --payload "$2" --query FunctionError --output text "$out")"; rc=$?
  RESULT="$(cat "$out" 2>/dev/null)"; rm -f "$out"
  (( rc == 0 )) && [[ "$err" == None || -z "$err" ]]
}
jq_py() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); print($2)" "$RESULT"; }

# ---- 1. Seed (skipped if the seed data is already compacted) ----
have=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "logs/dt=${DT}/" \
  --query 'length(not_null(Contents, `[]`))' --output text)
python3 - "$WORK" "$DT" "$PER_FILE" "${HOURS[*]}" "${SERVICES[*]}" "$have" <<'EOF'
import gzip, json, os, random, sys
from datetime import datetime, timezone
work, dt, per, hours, services, have = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4].split(), sys.argv[5].split(), int(sys.argv[6])
rnd = random.Random(20260925)  # fixed seed: re-runs pick the same sample ids
samples = []
for h in hours:
    base = int(datetime.strptime(f"{dt} {h}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc).timestamp() * 1e9)
    for svc in services:
        recs = []
        for i in range(per):
            tid, rid = f"{rnd.getrandbits(128):032x}", f"req-{rnd.getrandbits(64):016x}"
            recs.append({"timeUnixNano": str(base + i * (3590 * 10**9 // per)),
                         "severityNumber": 9, "severityText": "Info",
                         "body": {"stringValue": f"GET /api/{svc} 200"}, "traceId": tid,
                         "spanId": f"{rnd.getrandbits(64):016x}",
                         "attributes": [{"key": "request.id", "value": {"stringValue": rid}}]})
            if i == per // 2:
                samples.append((svc, h, tid, rid))
        if have == 0:
            doc = {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                                     "scopeLogs": [{"scope": {"name": "seed"}, "logRecords": recs}]}]}
            os.makedirs(f"{work}/seed/hour={h}", exist_ok=True)
            with gzip.open(f"{work}/seed/hour={h}/logs_seed-{svc}.json.gz", "wt") as f:
                f.write(json.dumps(doc))
with open(f"{work}/samples.tsv", "w") as f:
    for s in samples:
        f.write("\t".join(map(str, s)) + "\n")
EOF
if (( have == 0 )); then
  for h in "${HOURS[@]}"; do
    aws s3 cp --recursive --quiet "$WORK/seed/hour=${h}/" "s3://${BUCKET}/_incoming/logs/dt=${DT}/hour=${h}/"
  done
  pass "seeded ${#HOURS[@]} hours x ${#SERVICES[@]} services x ${PER_FILE} records"
  # Compact now rather than waiting for the schedule (leases make it safe if it runs too).
  for h in "${HOURS[@]}"; do
    invoke obs-compaction-dispatcher "{\"plan_only\":{\"signal\":\"logs\",\"dt\":\"${DT}\",\"hour\":\"${h}\"}}" \
      || { fail "planning ${h}:00: $RESULT"; exit 1; }
    for b in $(jq_py "' '.join(d['planned'])"); do
      invoke obs-compaction-worker "{\"signal\":\"logs\",\"dt\":\"${DT}\",\"hour\":\"${h}\",\"batch_id\":\"${b}\"}" \
        || { fail "compacting ${h}:00 chunk ${b}: $RESULT"; exit 1; }
    done
  done
  for _ in $(seq 60); do  # a scheduled worker may still hold a chunk
    left=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "_incoming/logs/dt=${DT}/" \
      --query 'length(not_null(Contents, `[]`))' --output text)
    (( left == 0 )) && break; sleep 5
  done
  (( left == 0 )) && pass "seed data compacted" || { fail "${left} raw seed files never compacted"; exit 1; }
else
  pass "seed data already compacted (${have} files); reusing it"
fi
TOTAL_FILES=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "logs/dt=${DT}/" \
  --query 'length(not_null(Contents, `[]`))' --output text)
echo "INFO  ${TOTAL_FILES} Parquet files under logs/dt=${DT}/"

lookup() {  # lookup <json> -> RESULT; prints files as "service min_ts"
  invoke obs-index-lookup "$1" || { fail "lookup $1: $RESULT"; return 1; }
}
n_files() { jq_py "len(d['files'])"; }

# ---- 2. Time-range pruning ----
lookup "{\"services\":[\"svc-a\"],\"start\":\"${DT}T10:30:00Z\",\"end\":\"${DT}T11:15:00Z\"}" && {
  got="$(jq_py "' '.join(f['min_ts'][11:13] for f in d['files'])")"
  [[ "$got" == "10 11" ]] && pass "svc-a 10:30-11:15 -> 2 files (10:00 and 11:00) of ${TOTAL_FILES}" \
    || fail "svc-a 10:30-11:15 returned hours [$got], expected [10 11]"; }

lookup "{\"services\":[\"svc-a\"],\"start\":\"${DT}T10:45:00Z\",\"end\":\"${DT}T10:46:00Z\"}" && {
  [[ "$(n_files)" == 1 ]] && pass "file starting before the range is included (10:45-10:46 -> the 10:00 file)" \
    || fail "10:45-10:46 returned $(n_files) files, expected 1"; }

lookup "{\"services\":[\"svc-a\"],\"start\":\"${DT}T11:00:00Z\",\"end\":\"${DT}T11:30:00Z\"}" && {
  got="$(jq_py "' '.join(f['min_ts'][11:13] for f in d['files'])")"
  [[ "$got" == "11" ]] && pass "file ending before the range is excluded (11:00-11:30 -> 11:00 file only)" \
    || fail "11:00-11:30 returned hours [$got], expected [11]"; }

# ---- 3. ID lookups across every service and hour ----
RANGE="\"start\":\"${DT}T10:00:00Z\",\"end\":\"${DT}T12:59:59Z\""
while IFS=$'\t' read -r svc h tid rid; do
  for field in trace_id request.id; do
    val=$tid; [[ $field == request.id ]] && val=$rid
    lookup "{${RANGE},\"match\":{\"${field}\":\"${val}\"}}" || continue
    cands=$(jq_py "d['stats']['in_time_range']"); kept=$(n_files)
    right=$(jq_py "any(f['service']=='${svc}' and f['min_ts'][11:13]=='${h}' for f in d['files'])")
    if [[ "$right" == True ]] && (( kept <= 2 )); then
      pass "${field} from ${svc} ${h}:00 -> ${kept} of ${cands} files, including the right one"
    else
      fail "${field} from ${svc} ${h}:00 -> ${kept} of ${cands} files, right one included: ${right}"
    fi
  done
done < <(awk "NR % 4 == 1" "$SAMPLES")  # svc-a 10:00, svc-b 11:00, svc-c 12:00

# ---- 4. Athena: the id really is in the file the index chose ----
read -r svc h tid rid < <(sed -n 2p "$SAMPLES")
lookup "{${RANGE},\"match\":{\"trace_id\":\"${tid}\"}}"
CHOSEN="$(jq_py "' '.join(f['file_path'] for f in d['files'])")"
QID="$(aws athena start-query-execution --work-group obs --query-string \
  "SELECT \"\$path\", count(*) FROM obs.logs WHERE dt = '${DT}' AND trace_id = '${tid}' GROUP BY 1" \
  --query QueryExecutionId --output text)"
while s=$(aws athena get-query-execution --query-execution-id "$QID" --query 'QueryExecution.Status.State' --output text); \
      [[ $s == QUEUED || $s == RUNNING ]]; do sleep 2; done
ACTUAL="$(aws athena get-query-results --query-execution-id "$QID" --query 'ResultSet.Rows[1:].Data[0].VarCharValue' --output text)"
read -r ATH_BYTES ATH_MS < <(aws athena get-query-execution --query-execution-id "$QID" \
  --query 'QueryExecution.Statistics.[DataScannedInBytes,EngineExecutionTimeInMillis]' --output text)
if [[ -n "$ACTUAL" && "$ACTUAL" != None && " $CHOSEN " == *" $ACTUAL "* ]]; then
  pass "Athena finds trace ${tid:0:8}... only in ${ACTUAL##*/}, which the index chose"
else
  fail "Athena finds the trace in [${ACTUAL}], index chose [${CHOSEN}]"
fi
echo "INFO  for comparison, Athena scanning every file for that trace: ${ATH_BYTES} bytes, ${ATH_MS} ms"

# ---- 5. Latency and cost (second call is warm) ----
lookup "{${RANGE},\"match\":{\"trace_id\":\"${tid}\"}}"
echo "INFO  warm lookup: $(jq_py "d['stats']['ms']") ms inside Lambda, $(jq_py "d['stats']['read_units']") DynamoDB read units"
units=$(aws cloudwatch get-metric-statistics --namespace AWS/DynamoDB --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=obs-index --start-time "$(date -u -d '-24 hours' +%FT%TZ)" \
  --end-time "$(date -u +%FT%TZ)" --period 86400 --statistics Sum --query 'Datapoints[0].Sum' --output text)
echo "INFO  obs-index read units, last 24h: ${units/None/0} (on-demand: \$0.125 per million)"

exit $FAILED
