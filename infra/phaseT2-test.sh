#!/usr/bin/env bash
# Phase T2 tests: authenticated, durable, autoscaling ingest, end to end
# through the public endpoint.
#
#   infra/phaseT2-test.sh
#
# Creates throwaway tenants (revoked at the end) and checks:
#   - the collector runs >= 2 healthy tasks across two AZs behind the NLB
#   - no key / wrong key -> rejected; a valid key -> 200 after the batch is written
#   - a tenant can't write as another: x-obs-tenant header and obs.tenant spoofing are ignored
#   - per-tenant throttling (tiny plan) returns 429s
#   - a revoked key is refused
#   - load through the endpoint while a collector task is stopped mid-run: no record lost
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

# ---- 1. Collector fleet ----
TG="$(aws elbv2 describe-target-groups --names obs-collector --query 'TargetGroups[0].TargetGroupArn' --output text)"
healthy="$(aws elbv2 describe-target-health --target-group-arn "$TG" \
  --query "TargetHealthDescriptions[?TargetHealth.State=='healthy'].Target.AvailabilityZone" --output text)"
n=$(wc -w <<<"$healthy"); azs=$(tr '\t' '\n' <<<"$healthy" | sort -u | grep -c .)
(( n >= 2 && azs >= 2 )) && pass "${n} healthy collector tasks across ${azs} AZs" \
  || fail "${n} healthy collector tasks across ${azs} AZs (want >= 2 across 2)"

# ---- 2. Tenants ----
KEY_A="$("$HERE/tenant.sh" create "$A" 2>/dev/null)" && KEY_B="$("$HERE/tenant.sh" create "$B" 2>/dev/null)" \
  && KEY_TINY="$("$HERE/tenant.sh" create "$TINY" test-tiny 2>/dev/null)" \
  || { fail "could not create test tenants"; exit 1; }
pass "created tenants ${A}, ${B}, ${TINY}"
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

# ---- 3. Authentication ----
read -r code _ <<<"$(post "" "" "$(body "" no-key)")"
[[ "$code" == 401 || "$code" == 403 ]] && pass "no API key -> HTTP ${code}" || fail "no API key -> HTTP ${code}"
read -r code _ <<<"$(post "obs_not-a-real-key" "" "$(body "" bad-key)")"
[[ "$code" == 401 || "$code" == 403 ]] && pass "unknown API key -> HTTP ${code}" || fail "unknown API key -> HTTP ${code}"
read -r code secs <<<"$(post "$KEY_A" "x-obs-tenant: ${B}" "$(body "$B" "a-pretending-to-be-b")")"
[[ "$code" == 200 ]] && pass "tenant ${A}'s key -> HTTP 200 after ${secs}s (answered once written)" \
  || fail "tenant ${A}'s key -> HTTP ${code}"
sleep 3
[[ "$(count "_incoming/tenant=${A}/")" -ge 1 && "$(count "_incoming/tenant=${B}/")" == 0 ]] \
  && pass "${A} sent x-obs-tenant: ${B} and obs.tenant=${B}; stored under ${A} only" \
  || fail "spoofing: ${A} objects $(count "_incoming/tenant=${A}/"), ${B} objects $(count "_incoming/tenant=${B}/")"

# ---- 4. Per-tenant throttling ----
codes="$(for i in $(seq 8); do { post "$KEY_TINY" "" "$(body "" "burst-$i")"; echo; } & done; wait)"
codes="$(cut -d' ' -f1 <<<"$codes" | tr '\n' ' ')"
n429=$(tr ' ' '\n' <<<"$codes" | grep -c '^429$')
(( n429 >= 1 )) && pass "tiny plan (1 req/s): 8 simultaneous requests -> ${n429} throttled (429)" \
  || fail "tiny plan: no 429s among [${codes}]"

# ---- 5. Revocation ----
"$HERE/tenant.sh" revoke "$TINY" >/dev/null
sleep 15
read -r code _ <<<"$(post "$KEY_TINY" "" "$(body "" after-revoke)")"
[[ "$code" == 401 || "$code" == 403 ]] && pass "revoked key -> HTTP ${code}" || fail "revoked key -> HTTP ${code}"

# ---- 6. Load, with a collector task stopped mid-run ----
CLUSTER="$(p1 ClusterName)"; SERVICE="$(p1 CollectorServiceName)"
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
aws ecs wait tasks-running --cluster "$CLUSTER" --tasks "$LG"
sleep 8
VICTIM="$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" --desired-status RUNNING --query 'taskArns[0]' --output text)"
aws ecs stop-task --cluster "$CLUSTER" --task "$VICTIM" --reason "phaseT2-test: stop mid-load" >/dev/null
info "stopped collector task ${VICTIM##*/} during the load (ECS sends SIGTERM, then replaces it)"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$LG"
rc="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$LG" --query 'tasks[0].containers[0].exitCode' --output text)"
[[ "$rc" == 0 ]] && pass "load generator sent ${SENT} records through the endpoint and exited cleanly" \
  || fail "load generator exit code ${rc} (see /obs/loadgen logs)"
sleep 20  # last flush

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
if [[ "$GOT" == "$SENT" ]]; then pass "all ${SENT} records stored under ${A}, none lost while a collector task was stopped"
elif (( GOT > SENT )); then info "${GOT} stored for ${SENT} sent: $((GOT - SENT)) duplicates from client retries (at-least-once)"; pass "no records lost"
else fail "${GOT} of ${SENT} records stored: $((SENT - GOT)) lost"; fi

running="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[0].runningCount' --output text)"
info "collector service back at ${running} running tasks"

exit $FAILED
