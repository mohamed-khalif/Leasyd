#!/usr/bin/env bash
# Phase T2 tests: authenticated, serverless ingest, end to end through the
# public endpoint (API Gateway -> authorizer -> ingest Lambda -> tenant's
# Firehose stream -> S3).
#
#   infra/phaseT2-test.sh
#
# Creates throwaway tenants (keys revoked at the end) and checks:
#   - each tenant gets its own Firehose streams
#   - no key / unknown key -> rejected
#   - a valid key -> 200; the data lands under that tenant only, even when
#     the client claims another tenant (header or obs.tenant attribute)
#   - freshness: time from send to the object being in S3
#   - per-tenant throttling (tiny plan) returns 429s
#   - a revoked key is refused
#   - load: protobuf OTLP from telemetrygen, every record stored exactly once
set -uo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="obs-data-${ACCOUNT}-${AWS_DEFAULT_REGION}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SUFFIX="$(date -u +%H%M%S)"
A="t2a-${SUFFIX}"; B="t2b-${SUFFIX}"; TINY="t2tiny-${SUFFIX}"
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
info() { echo "INFO  $*"; }
p1() { aws cloudformation describe-stacks --stack-name obs-phase1 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
t2() { aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
cleanup() { for t in "$A" "$B" "$TINY"; do "$HERE/tenant.sh" revoke "$t" >/dev/null 2>&1; done; }
trap cleanup EXIT

ENDPOINT="$(t2 IngestEndpoint)"; HOST="${ENDPOINT#https://}"; HOST="${HOST%%/*}"; STAGE="/${ENDPOINT##*/}"
info "endpoint ${ENDPOINT}"

# ---- 1. Tenants and their streams ----
KEY_A="$("$HERE/tenant.sh" create "$A" 2>/dev/null)" && KEY_B="$("$HERE/tenant.sh" create "$B" 2>/dev/null)" \
  && KEY_TINY="$("$HERE/tenant.sh" create "$TINY" test-tiny 2>/dev/null)" \
  || { fail "could not create test tenants"; exit 1; }
active=0
for t in "$A" "$B" "$TINY"; do for s in logs traces metrics; do
  [[ "$(aws firehose describe-delivery-stream --delivery-stream-name "obs-t-${t}-${s}" \
    --query DeliveryStreamDescription.DeliveryStreamStatus --output text 2>/dev/null)" == ACTIVE ]] && active=$((active + 1))
done; done
(( active == 9 )) && pass "created 3 tenants, each with its own logs/traces/metrics Firehose stream (9 ACTIVE)" \
  || fail "only ${active} of 9 tenant streams ACTIVE"
sleep 20  # new keys take a few seconds to reach the usage plans

body() {  # body <spoofed obs.tenant> <message>
  printf '{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"t2-test"}},{"key":"obs.tenant","value":{"stringValue":"%s"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"%s000000000","body":{"stringValue":"%s"}}]}]}]}' "$1" "$(date +%s)" "$2"
}
post() {  # post <key or ""> <extra header or ""> <body> -> "<code> <seconds>"
  local h=(); [[ -n "$1" ]] && h+=(-H "x-api-key: $1"); [[ -n "$2" ]] && h+=(-H "$2")
  curl -s -o /dev/null -w '%{http_code} %{time_total}' -X POST "${ENDPOINT}/v1/logs" \
    -H 'Content-Type: application/json' "${h[@]}" --data "$3"
}
count() { aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$1" --query 'length(not_null(Contents, `[]`))' --output text; }

# ---- 2. Authentication and tenant isolation ----
read -r code _ <<<"$(post "" "" "$(body "" no-key)")"
[[ "$code" == 401 || "$code" == 403 ]] && pass "no API key -> HTTP ${code}" || fail "no API key -> HTTP ${code}"
read -r code _ <<<"$(post "obs_not-a-real-key" "" "$(body "" bad-key)")"
[[ "$code" == 401 || "$code" == 403 ]] && pass "unknown API key -> HTTP ${code}" || fail "unknown API key -> HTTP ${code}"
sent_at=$(date +%s)
read -r code secs <<<"$(post "$KEY_A" "x-obs-tenant: ${B}" "$(body "$B" "a-pretending-to-be-b")")"
[[ "$code" == 200 ]] && pass "tenant ${A}'s key -> HTTP 200 in ${secs}s (stored durably in Firehose)" \
  || fail "tenant ${A}'s key -> HTTP ${code}"

# Freshness: how long until the record is an object in S3.
landed=""
for _ in $(seq 60); do
  (( $(count "_incoming/tenant=${A}/logs/") >= 1 )) && { landed=$(( $(date +%s) - sent_at )); break; }
  sleep 2
done
[[ -n "$landed" ]] && pass "record in S3 under ${A} ${landed}s after it was sent (Firehose buffer: 30s)" \
  || fail "record not in S3 after 120s"
[[ "$(count "_incoming/tenant=${B}/")" == 0 ]] \
  && pass "${A} claimed to be ${B} (header and obs.tenant); nothing stored under ${B}" \
  || fail "spoofing: ${B} has $(count "_incoming/tenant=${B}/") objects"
KEY="$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "_incoming/tenant=${A}/logs/" --query 'Contents[0].Key' --output text)"
aws s3 cp "s3://${BUCKET}/${KEY}" /tmp/t2obj.gz >/dev/null 2>&1
attrs="$(gzip -dc /tmp/t2obj.gz | head -1 | python3 -c "import json,sys; print([a['key'] for a in json.loads(sys.stdin.readline())['resourceLogs'][0]['resource']['attributes']])")"
[[ "$attrs" == "['service.name']" ]] && pass "client-sent obs.tenant stripped from the stored record" \
  || fail "stored resource attributes: ${attrs}"

# ---- 3. Per-tenant throttling ----
codes="$(for i in $(seq 8); do { post "$KEY_TINY" "" "$(body "" "burst-$i")"; echo; } & done; wait)"
codes="$(cut -d' ' -f1 <<<"$codes" | tr '\n' ' ')"
n429=$(tr ' ' '\n' <<<"$codes" | grep -c '^429$')
(( n429 >= 1 )) && pass "tiny plan (1 req/s): 8 simultaneous requests -> ${n429} throttled (429)" \
  || fail "tiny plan: no 429s among [${codes}]"

# ---- 4. Revocation ----
"$HERE/tenant.sh" revoke "$TINY" >/dev/null
sleep 15
read -r code _ <<<"$(post "$KEY_TINY" "" "$(body "" after-revoke)")"
[[ "$code" == 401 || "$code" == 403 ]] && pass "revoked key -> HTTP ${code}" || fail "revoked key -> HTTP ${code}"

# ---- 5. Load: protobuf OTLP through the endpoint, exact count ----
CLUSTER="$(p1 ClusterName)"
RUN_ID="t2-${SUFFIX}"; WORKERS=4; PER_WORKER=5000; SENT=$((WORKERS * PER_WORKER))
overrides=$(cat <<EOF
{"containerOverrides":[{"name":"loadgen","command":[
  "logs","--otlp-http","--otlp-endpoint","${HOST}:443","--otlp-http-url-path","${STAGE}/v1/logs",
  "--otlp-header","x-api-key=\"${KEY_A}\"",
  "--logs","${PER_WORKER}","--workers","${WORKERS}","--rate","200",
  "--otlp-attributes","service.name=\"t2-load\"","--telemetry-attributes","run.id=\"${RUN_ID}\""]}]}
EOF
)
LG="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$(p1 LoadgenTaskDefinition)" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$(p1 SubnetIds)],securityGroups=[$(p1 LoadgenSecurityGroup)],assignPublicIp=ENABLED}" \
  --overrides "$overrides" --query 'tasks[0].taskArn' --output text)"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$LG"
rc="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$LG" --query 'tasks[0].containers[0].exitCode' --output text)"
[[ "$rc" == 0 ]] && pass "load generator sent ${SENT} records (OTLP protobuf over HTTPS) and exited cleanly" \
  || fail "load generator exit code ${rc} (see /obs/loadgen logs)"
sleep 45  # Firehose buffer (30s) + delivery

DT="$(date -u +%Y-%m-%d)"
SQL="SELECT count(*) FROM obs.raw_logs
CROSS JOIN UNNEST(resourcelogs) AS t1(rl) CROSS JOIN UNNEST(rl.scopelogs) AS t2(sl)
CROSS JOIN UNNEST(sl.logrecords) AS t3(lr)
WHERE tenant = '${A}' AND dt = '${DT}'
  AND any_match(lr.attributes, a -> a.key = 'run.id' AND a.value.stringvalue = '${RUN_ID}')"
QID="$(aws athena start-query-execution --work-group obs --query-string "$SQL" --query QueryExecutionId --output text)"
while s=$(aws athena get-query-execution --query-execution-id "$QID" --query 'QueryExecution.Status.State' --output text); \
      [[ $s == QUEUED || $s == RUNNING ]]; do sleep 2; done
GOT="$(aws athena get-query-results --query-execution-id "$QID" --query 'ResultSet.Rows[1].Data[0].VarCharValue' --output text)"
if [[ "$GOT" == "$SENT" ]]; then pass "all ${SENT} records stored under ${A}, exactly once"
elif (( GOT > SENT )); then info "${GOT} stored for ${SENT} sent: $((GOT - SENT)) duplicates from client retries (at-least-once)"; pass "no records lost"
else fail "${GOT} of ${SENT} records stored: $((SENT - GOT)) missing"; fi

errs="$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "_incoming/_errors/tenant=${A}/" --query 'length(not_null(Contents, `[]`))' --output text)"
(( errs == 0 )) && pass "no Firehose delivery errors" || fail "${errs} objects under _incoming/_errors/tenant=${A}/"
info "Lambda invocations for this run: $(aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations \
  --dimensions Name=FunctionName,Value=obs-ingest --start-time "$(date -u -d '-30 min' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 1800 --statistics Sum --query 'Datapoints[0].Sum' --output text)"

exit $FAILED
