#!/usr/bin/env bash
# Phase 1 tests: send a known number of log records through the collector,
# check the S3 layout, and count them back with Athena.
#
# Usage: infra/phase1-test.sh [stack-name]
set -uo pipefail

STACK="${1:-obs-phase1}"
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="obs-data-${ACCOUNT}-${AWS_DEFAULT_REGION}"
RUN_ID="run-$(date -u +%Y%m%dT%H%M%S)"
BATCH_WAIT="${BATCH_WAIT:-90}"   # collector batch timeout (60s) + upload margin
FAILED=0

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }

CLUSTER="$(output ClusterName)"
SERVICE="$(output CollectorServiceName)"
LOADGEN_TD="$(output LoadgenTaskDefinition)"
SUBNETS="$(output SubnetIds)"
LOADGEN_SG="$(output LoadgenSecurityGroup)"
WORKGROUP="$(output AthenaWorkGroup)"

# ---- 1. Collector is running; find its private IP ----
TASK="$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" --desired-status RUNNING \
  --query 'taskArns[0]' --output text)"
if [[ -z "$TASK" || "$TASK" == None ]]; then
  echo "FAIL  no running collector task (is CollectorDesiredCount 0?)"; exit 1
fi
aws ecs wait tasks-running --cluster "$CLUSTER" --tasks "$TASK"
COLLECTOR_IP="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" \
  --query "tasks[0].attachments[0].details[?name=='privateIPv4Address'].value | [0]" --output text)"
pass "collector running at $COLLECTOR_IP"

# ---- 2. Generate a known number of log records from two services ----
declare -A SENT=( [loadgen-a]=3000 [loadgen-b]=2000 )
started=$(date -u +%s)
LOADGEN_TASKS=()
for svc in "${!SENT[@]}"; do
  overrides=$(cat <<EOF
{"containerOverrides":[{"name":"loadgen","command":[
  "logs","--otlp-endpoint","${COLLECTOR_IP}:4317","--otlp-insecure",
  "--logs","${SENT[$svc]}","--workers","1","--rate","0",
  "--otlp-attributes","service.name=\"${svc}\"",
  "--telemetry-attributes","run.id=\"${RUN_ID}\""]}]}
EOF
)
  t="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$LOADGEN_TD" --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${LOADGEN_SG}],assignPublicIp=ENABLED}" \
    --overrides "$overrides" --query 'tasks[0].taskArn' --output text)"
  LOADGEN_TASKS+=("$t")
done
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "${LOADGEN_TASKS[@]}"
codes="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "${LOADGEN_TASKS[@]}" \
  --query 'tasks[].containers[0].exitCode' --output text)"
if [[ "$codes" =~ ^[0[:space:]]+$ ]]; then pass "load generators exited cleanly"; else fail "load generator exit codes: $codes"; fi

echo "Waiting ${BATCH_WAIT}s for the collector to flush its batch to S3..."
sleep "$BATCH_WAIT"

# ---- 3. Files landed under the right prefixes ----
DAYS=("$(date -u -d "@$started" +%Y-%m-%d)")
[[ "$(date -u +%Y-%m-%d)" != "${DAYS[0]}" ]] && DAYS+=("$(date -u +%Y-%m-%d)")
KEYS=""
for d in "${DAYS[@]}"; do
  KEYS+="$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "_incoming/logs/dt=${d}/" \
    --query 'Contents[].Key' --output text 2>/dev/null | tr '\t' '\n' | grep -v '^None$')"$'\n'
done
KEYS="$(sed '/^$/d' <<<"$KEYS")"
n_keys=$(grep -c . <<<"$KEYS")
bad=$(grep -cvE '^_incoming/logs/dt=[0-9]{4}-[0-9]{2}-[0-9]{2}/hour=([01][0-9]|2[0-3])/logs_[0-9a-f-]+\.json\.gz$' <<<"$KEYS")
if (( n_keys > 0 && bad == 0 )); then pass "$n_keys file(s) under _incoming/logs/dt=/hour=/, all names well-formed"
else fail "$n_keys file(s), $bad with unexpected keys:"; grep -vE 'logs_[0-9a-f-]+\.json\.gz$' <<<"$KEYS" | head -5; fi

# ---- 4. Athena reads them back with the right counts ----
dt_list="$(printf "'%s'," "${DAYS[@]}")"; dt_list="${dt_list%,}"
SQL="
SELECT element_at(filter(rl.resource.attributes, a -> a.key = 'service.name'), 1).value.stringvalue AS service,
       count(*) AS records
FROM obs.raw_logs
CROSS JOIN UNNEST(resourcelogs) AS t1(rl)
CROSS JOIN UNNEST(rl.scopelogs) AS t2(sl)
CROSS JOIN UNNEST(sl.logrecords) AS t3(lr)
WHERE dt IN (${dt_list})
  AND any_match(lr.attributes, a -> a.key = 'run.id' AND a.value.stringvalue = '${RUN_ID}')
GROUP BY 1 ORDER BY 1"

QID="$(aws athena start-query-execution --work-group "$WORKGROUP" --query-string "$SQL" \
  --query QueryExecutionId --output text)"
while :; do
  state="$(aws athena get-query-execution --query-execution-id "$QID" --query 'QueryExecution.Status.State' --output text)"
  [[ "$state" == QUEUED || "$state" == RUNNING ]] || break
  sleep 2
done
if [[ "$state" != SUCCEEDED ]]; then
  fail "Athena query $state: $(aws athena get-query-execution --query-execution-id "$QID" \
    --query 'QueryExecution.Status.StateChangeReason' --output text)"
else
  declare -A GOT=()
  while read -r svc cnt; do [[ -n "$svc" ]] && GOT[$svc]=$cnt; done < <(
    aws athena get-query-results --query-execution-id "$QID" \
      --query 'ResultSet.Rows[1:].[Data[0].VarCharValue,Data[1].VarCharValue]' --output text)
  for svc in "${!SENT[@]}"; do
    if [[ "${GOT[$svc]:-0}" == "${SENT[$svc]}" ]]; then pass "Athena counts ${SENT[$svc]} records for $svc"
    else fail "Athena counts ${GOT[$svc]:-0} records for $svc, sent ${SENT[$svc]}"; fi
  done
  read -r scanned ms < <(aws athena get-query-execution --query-execution-id "$QID" \
    --query 'QueryExecution.Statistics.[DataScannedInBytes,EngineExecutionTimeInMillis]' --output text)
  echo "BASELINE  raw-JSON query scanned ${scanned} bytes in ${ms} ms (query ${QID})"
fi

# ---- 5. S3 5xx / throttling on _incoming/ (request metrics lag ~15 min) ----
errs="$(aws cloudwatch get-metric-statistics --namespace AWS/S3 --metric-name 5xxErrors \
  --dimensions Name=BucketName,Value="$BUCKET" Name=FilterId,Value=incoming \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum --query 'Datapoints[0].Sum' --output text)"
echo "INFO  S3 5xx errors on _incoming/ in the last hour: ${errs/None/no data yet}"

echo "run id: $RUN_ID"
exit $FAILED
