#!/usr/bin/env bash
# Phase 2 tests.
#
#   infra/phase2-test.sh load [hours]   start steady load (2 services x 20 logs/s) for N hours (default 3)
#   infra/phase2-test.sh compare [YYYY-MM-DD HH]
#                                      once at least one full hour of load has closed
#                                      (defaults to the most recent closed hour):
#                                        - Athena baseline on that hour's raw files
#                                        - hour planned into chunks; one worker crashes
#                                          mid-delete, then every chunk runs
#                                        - no rows lost or duplicated, index matches files
#                                        - Athena on the compacted Parquet, bytes/time vs baseline
#
# Deploy obs-phase2 with ScheduleState=DISABLED before `load`, so the schedule
# doesn't compact the test hour first; re-enable it after `compare`.
set -uo pipefail

MODE="${1:-}"
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="obs-data-${ACCOUNT}-${AWS_DEFAULT_REGION}"
TENANT="${TENANT:-default}"   # tenant under test (see infra/tenant.sh)
WORKER=obs-compaction-worker
TABLE=obs-index
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
p1() {
  aws cloudformation describe-stacks --stack-name obs-phase1 \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

athena() {  # athena <sql>  -> sets QID, prints nothing; returns non-zero on failure
  QID="$(aws athena start-query-execution --work-group obs --query-string "$1" --query QueryExecutionId --output text)"
  local state
  while :; do
    state="$(aws athena get-query-execution --query-execution-id "$QID" --query 'QueryExecution.Status.State' --output text)"
    [[ "$state" == QUEUED || "$state" == RUNNING ]] || break
    sleep 2
  done
  if [[ "$state" != SUCCEEDED ]]; then
    echo "      Athena $state: $(aws athena get-query-execution --query-execution-id "$QID" \
      --query 'QueryExecution.Status.StateChangeReason' --output text)"
    return 1
  fi
}
athena_rows() { aws athena get-query-results --query-execution-id "$QID" \
  --query 'ResultSet.Rows[1:].Data[*].VarCharValue' --output text; }
athena_stats() { aws athena get-query-execution --query-execution-id "$QID" \
  --query 'QueryExecution.Statistics.[DataScannedInBytes,EngineExecutionTimeInMillis]' --output text; }

# AWS CLI v2 needs --cli-binary-format to send a JSON payload as-is; v1
# rejects the flag (and sends raw JSON by default).
PAYLOAD_FMT=()
[[ "$(aws --version 2>&1)" == aws-cli/1.* ]] || PAYLOAD_FMT=(--cli-binary-format raw-in-base64-out)

invoke_lambda() {  # invoke_lambda <function> <payload-json> -> sets RESULT; fails on CLI error or FunctionError
  local out err rc; out="$(mktemp)"
  err="$(aws lambda invoke --function-name "$1" "${PAYLOAD_FMT[@]}" \
    --cli-read-timeout 900 --payload "$2" --query FunctionError --output text "$out")"; rc=$?
  RESULT="$(cat "$out" 2>/dev/null)"; rm -f "$out"
  (( rc == 0 )) || { RESULT="aws cli exited $rc${RESULT:+: $RESULT}"; return 1; }
  [[ "$err" == None || -z "$err" ]]
}
invoke_worker() { invoke_lambda "$WORKER" "$1"; }

# ------------------------------------------------------------------- load
if [[ "$MODE" == load ]]; then
  # Through the authenticated endpoint, as a real tenant would send.
  # Needs API_KEY (from infra/tenant.sh create) and TENANT set to that key's tenant.
  : "${API_KEY:?set API_KEY to a key from infra/tenant.sh create \$TENANT}"
  HOURS="${2:-3}"
  CLUSTER="$(p1 ClusterName)"
  ENDPOINT="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
    --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)"
  HOST="${ENDPOINT#https://}"; HOST="${HOST%%/*}"; STAGE="/${ENDPOINT##*/}"
  RUN_ID="load-$(date -u +%Y%m%dT%H%M%S)"
  for svc in loadgen-a loadgen-b; do
    aws ecs run-task --cluster "$CLUSTER" --task-definition "$(p1 LoadgenTaskDefinition)" --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[$(p1 SubnetIds)],securityGroups=[$(p1 LoadgenSecurityGroup)],assignPublicIp=ENABLED}" \
      --overrides "{\"containerOverrides\":[{\"name\":\"loadgen\",\"command\":[\"logs\",\"--otlp-http\",\"--otlp-endpoint\",\"${HOST}:443\",\"--otlp-http-url-path\",\"${STAGE}/v1/logs\",\"--otlp-header\",\"x-api-key=\\\"${API_KEY}\\\"\",\"--duration\",\"${HOURS}h\",\"--rate\",\"20\",\"--workers\",\"1\",\"--otlp-attributes\",\"service.name=\\\"${svc}\\\"\",\"--telemetry-attributes\",\"run.id=\\\"${RUN_ID}\\\"\"]}]}" \
      --query 'tasks[0].taskArn' --output text
  done
  echo "Started ${HOURS}h of load for tenant ${TENANT}, run id ${RUN_ID}. Run 'TENANT=${TENANT} $0 compare' once a full hour has closed (+10 min)."
  exit 0
fi

[[ "$MODE" == compare ]] || { sed -n '2,13p' "$0"; exit 2; }

# ---------------------------------------------------------------- compare
# Use the hour given (compare YYYY-MM-DD HH), else the most recent raw logs
# hour that closed at least 10 minutes ago: the one most likely to be a full
# hour of load, rather than leftovers from earlier tests.
NOW=$(date -u +%s)
TARGET=""
if [[ $# -ge 3 ]]; then
  TARGET="$2 $3"
else
  for dtp in $(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "_incoming/tenant=${TENANT}/logs/" --delimiter / \
                 --query 'CommonPrefixes[].Prefix' --output text); do
    for hp in $(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$dtp" --delimiter / \
                  --query 'CommonPrefixes[].Prefix' --output text); do
      dt="${dtp#*dt=}"; dt="${dt%/}"; hr="${hp#*hour=}"; hr="${hr%/}"
      end=$(( $(date -u -d "$dt $hr:00" +%s) + 3600 + 600 ))
      (( end <= NOW )) && TARGET="$dt $hr"   # prefixes are listed oldest first
    done
  done
fi
[[ -z "$TARGET" ]] && { echo "No closed raw hour yet: wait until an hour has fully passed + 10 min."; exit 1; }
read -r DT HR <<<"$TARGET"
PREFIX="_incoming/tenant=${TENANT}/logs/dt=${DT}/hour=${HR}/"
echo "Test hour: dt=${DT} hour=${HR}"

read -r RAW_FILES RAW_BYTES < <(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX" \
  --query '[length(Contents), sum(Contents[].Size)]' --output text)
echo "INFO  raw: ${RAW_FILES} files, ${RAW_BYTES} bytes"

# 1. Baseline: count by service on the raw hour.
RAW_SQL="
SELECT element_at(filter(rl.resource.attributes, a -> a.key = 'service.name'), 1).value.stringvalue AS service,
       count(*) FROM obs.raw_logs
CROSS JOIN UNNEST(resourcelogs) AS t1(rl) CROSS JOIN UNNEST(rl.scopelogs) AS t2(sl)
CROSS JOIN UNNEST(sl.logrecords) AS t3(lr)
WHERE tenant = '${TENANT}' AND dt = '${DT}' AND hour = '${HR}' GROUP BY 1 ORDER BY 1"
athena "$RAW_SQL" || { fail "raw baseline query"; exit 1; }
RAW_COUNTS="$(athena_rows)"; read -r RAW_SCANNED RAW_MS < <(athena_stats)
RAW_TOTAL=$(awk '{s+=$2} END {print s+0}' <<<"$RAW_COUNTS")
echo "INFO  raw rows: ${RAW_TOTAL}; query scanned ${RAW_SCANNED} bytes in ${RAW_MS} ms"

# 2. Plan the hour, crash the first chunk's worker mid-delete, then run every chunk.
DISPATCHER=obs-compaction-dispatcher
invoke_lambda "$DISPATCHER" "{\"plan_only\":{\"tenant\":\"${TENANT}\",\"signal\":\"logs\",\"dt\":\"${DT}\",\"hour\":\"${HR}\"}}" \
  || { fail "planning failed: $RESULT"; exit 1; }
BATCHES="$(python3 -c 'import json,sys; print(" ".join(json.loads(sys.argv[1])["planned"]))' "$RESULT")"
read -ra BATCH_LIST <<<"$BATCHES"
(( ${#BATCH_LIST[@]} > 0 )) && pass "hour planned as ${#BATCH_LIST[@]} chunk(s): ${BATCHES}" || { fail "no chunks planned"; exit 1; }

EVENT="\"tenant\":\"${TENANT}\",\"signal\":\"logs\",\"dt\":\"${DT}\",\"hour\":\"${HR}\""
if invoke_worker "{${EVENT},\"batch_id\":\"${BATCH_LIST[0]}\",\"crash_after\":\"partial_delete\"}"; then
  fail "crash injection did not crash (is AllowCrashInjection=true?)"
else
  left=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX" --query 'length(not_null(Contents, `[]`))' --output text)
  pass "worker crashed after deleting part of its inputs (${left/None/0} of ${RAW_FILES} raw files left)"
fi
for b in "${BATCH_LIST[@]}"; do
  if invoke_worker "{${EVENT},\"batch_id\":\"${b}\"}"; then
    pass "chunk ${b} compacted"; echo "      ${RESULT:0:300}"
  else
    fail "chunk ${b} failed: $RESULT"; exit 1
  fi
done

left=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX" --query 'length(not_null(Contents, `[]`))' --output text)
[[ "$left" == None || "$left" == 0 ]] && pass "raw hour fully consumed" || fail "${left} raw files still in ${PREFIX}"

# 3. Parquet files and index entries for the hour's chunks.
PQ_KEYS=""; IDX=""
for b in "${BATCH_LIST[@]}"; do
  PQ_KEYS+="$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "data/tenant=${TENANT}/logs/" \
    --query "Contents[?contains(Key, 'part-${b}-')].[Key,Size]" --output text)"$'\n'
  IDX+="$(aws dynamodb scan --table-name "$TABLE" --filter-expression 'batch_id = :b' \
    --expression-attribute-values "{\":b\":{\"S\":\"${b}\"}}" \
    --query 'Items[].[row_count.N, min_ts.S, max_ts.S]' --output text)"$'\n'
done
PQ_KEYS="$(sed '/^$/d' <<<"$PQ_KEYS")"; IDX="$(sed '/^$/d' <<<"$IDX")"
PQ_FILES=$(grep -c . <<<"$PQ_KEYS"); PQ_BYTES=$(awk '{s+=$2} END {print s+0}' <<<"$PQ_KEYS")
IDX_N=$(grep -c . <<<"$IDX"); IDX_ROWS=$(awk '{s+=$1} END {print s+0}' <<<"$IDX")
(( IDX_N == PQ_FILES )) && pass "${PQ_FILES} Parquet file(s), ${IDX_N} index entr(ies)" \
  || fail "${PQ_FILES} Parquet files but ${IDX_N} index entries"
(( IDX_ROWS == RAW_TOTAL )) && pass "index row_count sums to ${IDX_ROWS} = raw rows" \
  || fail "index row_count ${IDX_ROWS} != raw rows ${RAW_TOTAL}"
bad_span=$(awk '{ if (substr($2,1,13) != substr($3,1,13)) n++ } END {print n+0}' <<<"$IDX")
(( bad_span == 0 )) && pass "every file spans at most one hour" || fail "${bad_span} file(s) span more than one hour"
leftover=$(aws dynamodb query --table-name "$TABLE" --key-condition-expression 'pk = :p' \
  --expression-attribute-values "{\":p\":{\"S\":\"_plan#${TENANT}#logs#${DT}#${HR}\"}}" --query Count --output text)
(( leftover == 0 )) && pass "no chunk plan left behind" || fail "${leftover} plan(s) left for the hour"

# 4. Same question against the compacted Parquet.
DTS="$(awk '{print "'"'"'" substr($2,1,10) "'"'"'"}' <<<"$IDX" | sort -u | paste -sd,)"
HRS="$(awk '{print "'"'"'" substr($2,12,2) "'"'"'"}' <<<"$IDX" | sort -u | paste -sd,)"
BATCH_RE="$(IFS='|'; echo "${BATCH_LIST[*]}")"
PQ_SQL="SELECT service, count(*) FROM obs.logs
WHERE tenant = '${TENANT}' AND dt IN (${DTS}) AND hour IN (${HRS}) AND regexp_like(\"\$path\", 'part-(${BATCH_RE})-[0-9]{3}\.parquet\$')
GROUP BY 1 ORDER BY 1"
athena "$PQ_SQL" || { fail "compacted query"; exit 1; }
PQ_COUNTS="$(athena_rows)"; read -r PQ_SCANNED PQ_MS < <(athena_stats)
[[ "$PQ_COUNTS" == "$RAW_COUNTS" ]] && pass "per-service counts identical before and after compaction" \
  || { fail "counts differ"; echo "raw:"; echo "$RAW_COUNTS"; echo "compacted:"; echo "$PQ_COUNTS"; }

echo
echo "                 files      bytes stored   query scanned   query time"
printf "raw JSON     %9s %16s %15s %10s ms\n" "$RAW_FILES" "$RAW_BYTES" "$RAW_SCANNED" "$RAW_MS"
printf "Parquet      %9s %16s %15s %10s ms\n" "$PQ_FILES" "$PQ_BYTES" "$PQ_SCANNED" "$PQ_MS"
echo "(Athena bills at least 10 MB per query, so small test hours cost the same either way.)"
exit $FAILED
