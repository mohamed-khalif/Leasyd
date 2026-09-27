#!/usr/bin/env bash
# Phase T4 tests: traces and metrics, end to end on AWS.
#
#   infra/phaseT4-test.sh
#
# Creates a throwaway tenant, sends spans and metric points (gauge, sum,
# histogram) through the public endpoint, and checks for each signal:
#   - freshness: how long until a lookup finds them (fast lane, raw files)
#   - a trace id lookup finds the spans' file, and an unknown one finds none
#   - after compacting, the same lookup returns Parquet only, same row count
#   - Athena reads the Parquet through obs.traces / obs.metrics with the
#     expected counts, including nested span events and histogram buckets
set -uo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
HERE="$(cd "$(dirname "$0")" && pwd)"
T="t4-$(date -u +%H%M%S)"
SPANS=300; TRACED=3            # TRACED spans share one known trace id
GAUGE=100; SUM=50; HIST=50     # metric points by type
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
info() { echo "INFO  $*"; }
trap '"$HERE/tenant.sh" revoke "$T" >/dev/null 2>&1' EXIT

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
athena() {  # athena <sql> -> prints result rows (tab-separated); non-zero on failure
  local qid state
  qid="$(aws athena start-query-execution --work-group obs --query-string "$1" --query QueryExecutionId --output text)" || return 1
  while :; do
    state="$(aws athena get-query-execution --query-execution-id "$qid" --query 'QueryExecution.Status.State' --output text)"
    [[ "$state" == QUEUED || "$state" == RUNNING ]] || break
    sleep 2
  done
  if [[ "$state" != SUCCEEDED ]]; then
    aws athena get-query-execution --query-execution-id "$qid" --query 'QueryExecution.Status.StateChangeReason' --output text >&2
    return 1
  fi
  aws athena get-query-results --query-execution-id "$qid" --query 'ResultSet.Rows[1:].Data[*].VarCharValue' --output text
}

ENDPOINT="$(aws cloudformation describe-stacks --stack-name obs-phaseT2 \
  --query "Stacks[0].Outputs[?OutputKey=='IngestEndpoint'].OutputValue" --output text)"
KEY="$("$HERE/tenant.sh" create "$T" 2>/dev/null)" || { fail "could not create tenant"; exit 1; }
info "tenant ${T}; waiting for its key to become active"
# A new key flickers between 200 and 403 while API Gateway propagates it: wait for 5 in a row.
ok=0
for _ in $(seq 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${ENDPOINT}/v1/traces" -H "x-api-key: ${KEY}" \
    -H 'Content-Type: application/json' --data '{"resourceSpans":[]}')
  if [[ "$code" == 200 ]]; then (( ++ok >= 5 )) && break; else ok=0; fi
  sleep 3
done
(( ok >= 5 )) || { fail "key not active (HTTP ${code})"; exit 1; }

# ---- 1. Send spans and metric points ----
TRACE="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
NOW_NS="$(date +%s)000000000"
gen() { python3 - "$@" <<'EOF'
import json, secrets, sys
kind, now, trace = sys.argv[1], int(sys.argv[2]), sys.argv[3]
spans, traced, gauge, n_sum, hist = map(int, sys.argv[4:9])
res = {"attributes": [{"key": "service.name", "value": {"stringValue": "t4-svc"}}]}
if kind == "traces":
    items = [{"traceId": trace if i < traced else secrets.token_hex(16), "spanId": secrets.token_hex(8),
              "name": f"op-{i % 5}", "kind": 2, "startTimeUnixNano": str(now + i * 1000),
              "endTimeUnixNano": str(now + i * 1000 + 250_000),
              "status": {"code": 2 if i % 10 == 0 else 1},
              "events": [{"timeUnixNano": str(now + i * 1000 + 10), "name": "retry"}] if i % 2 == 0 else [],
              "attributes": [{"key": "http.route", "value": {"stringValue": f"/r{i % 3}"}}]}
             for i in range(spans)]
    print(json.dumps({"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": items}]}]}))
else:
    pt = lambda i, **kw: {"timeUnixNano": str(now + i * 1000), "startTimeUnixNano": str(now), **kw}
    metrics = [
        {"name": "t4.cpu", "unit": "1", "gauge": {"dataPoints": [pt(i, asDouble=i / 100) for i in range(gauge)]}},
        {"name": "t4.reqs", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                                    "dataPoints": [pt(i, asInt=str(i)) for i in range(n_sum)]}},
        {"name": "t4.latency", "unit": "ms", "histogram": {"aggregationTemporality": 1, "dataPoints": [
            pt(i, count="6", sum=21.5, bucketCounts=["1", "2", "3"], explicitBounds=[10, 100]) for i in range(hist)]}},
    ]
    print(json.dumps({"resourceMetrics": [{"resource": res, "scopeMetrics": [{"metrics": metrics}]}]}))
EOF
}
sent=$(date +%s)
for sig in traces metrics; do
  code=$(gen "$sig" "$NOW_NS" "$TRACE" $SPANS $TRACED $GAUGE $SUM $HIST | curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${ENDPOINT}/v1/${sig}" -H "x-api-key: ${KEY}" -H 'Content-Type: application/json' --data-binary @-)
  [[ "$code" == 200 ]] && pass "sent ${sig}" || { fail "send ${sig} -> HTTP ${code}"; exit 1; }
done

START="$(date -u -d '-10 min' +%FT%TZ)"; END="$(date -u -d '+10 min' +%FT%TZ)"
q() { echo "{\"tenant\":\"${T}\",\"signal\":\"$1\",\"services\":[\"t4-svc\"],\"start\":\"${START}\",\"end\":\"${END}\"${2:-}}"; }
rows_sum="sum(f['row_count'] for f in d['files'])"
kinds="sorted({f['kind'] for f in d['files']})"
declare -A WANT=([traces]=$SPANS [metrics]=$(( GAUGE + SUM + HIST ))) DT HR

# ---- 2. Freshness and fast-lane lookups ----
for sig in traces metrics; do
  found=""
  while (( $(date +%s) - sent < 180 )); do
    invoke obs-index-lookup "$(q "$sig")" && [[ "$(py "$rows_sum")" -ge "${WANT[$sig]}" ]] \
      && { found=$(( $(date +%s) - sent )); break; }
    sleep 3
  done
  [[ -n "$found" ]] && pass "${sig} findable ${found}s after sending (target: 60s)" \
    || { fail "${sig} not findable after 180s"; exit 1; }
  [[ "$(py "$kinds")" == "['raw']" && "$(py "$rows_sum")" == "${WANT[$sig]}" ]] \
    && pass "${sig}: ${WANT[$sig]} rows from raw files" \
    || fail "${sig} before compaction: kinds $(py "$kinds"), rows $(py "$rows_sum") (want ['raw'], ${WANT[$sig]})"
  path="$(py "d['files'][0]['file_path']")"
  DT[$sig]="$(sed -E 's#.*/dt=([0-9-]+)/.*#\1#' <<<"$path")"; HR[$sig]="$(sed -E 's#.*/hour=([0-9]+)/.*#\1#' <<<"$path")"
done
invoke obs-index-lookup "$(q traces ",\"match\":{\"trace_id\":\"${TRACE}\"}")"
[[ "$(py "len(d['files'])")" -ge 1 ]] && pass "trace id lookup finds the raw spans file" || fail "trace id lookup found nothing"
invoke obs-index-lookup "$(q traces ",\"match\":{\"trace_id\":\"$(python3 -c 'import secrets; print(secrets.token_hex(16))')\"}")"
[[ "$(py "len(d['files'])")" == 0 ]] && pass "an unknown trace id finds no spans file" \
  || info "unknown trace id matched $(py "len(d['files'])") file(s) (1% false positives are expected)"

# ---- 3. Compact, then look again ----
for sig in traces metrics; do
  ev="\"tenant\":\"${T}\",\"signal\":\"${sig}\",\"dt\":\"${DT[$sig]}\",\"hour\":\"${HR[$sig]}\""
  invoke obs-compaction-dispatcher "{\"plan_only\":{${ev}}}" || { fail "planning ${sig}: $RESULT"; exit 1; }
  for b in $(py "' '.join(d['planned'])"); do
    invoke obs-compaction-worker "{${ev},\"batch_id\":\"${b}\"}" || { fail "compacting ${sig} ${b}: $RESULT"; exit 1; }
  done
  invoke obs-index-lookup "$(q "$sig")"
  [[ "$(py "$kinds")" == "['parquet']" && "$(py "$rows_sum")" == "${WANT[$sig]}" ]] \
    && pass "${sig} after compaction: ${WANT[$sig]} rows, all from Parquet" \
    || fail "${sig} after compaction: kinds $(py "$kinds"), rows $(py "$rows_sum") (want ['parquet'], ${WANT[$sig]})"
  left="$(aws dynamodb query --table-name obs-index --key-condition-expression 'pk = :p' \
    --expression-attribute-values "{\":p\":{\"S\":\"${T}#_raw#${sig}#${DT[$sig]}#${HR[$sig]}\"}}" --query Count --output text)"
  (( left == 0 )) && pass "${sig} fast-lane records retired" || fail "${sig}: ${left} raw-file records left"
done
invoke obs-index-lookup "$(q traces ",\"match\":{\"trace_id\":\"${TRACE}\"}")"
[[ "$(py "[f['kind'] for f in d['files']]")" == "['parquet']" ]] && pass "trace id now found in the Parquet spans file" \
  || fail "trace id after compaction: $(py "[f['kind'] for f in d['files']]")"

# ---- 4. Athena reads the Parquet ----
norm() { tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//'; }
DAYS="'$(date -u -d '-1 day' +%F)','$(date -u +%F)','$(date -u -d '+1 day' +%F)'"
got="$(athena "SELECT count(*), count_if(trace_id = '${TRACE}'), count_if(status_code = 2),
  sum(cardinality(events)), max(duration_ns), count(DISTINCT name)
  FROM obs.traces WHERE tenant = '${T}' AND dt IN (${DAYS})" | norm)" || fail "Athena traces query"
want="${SPANS} ${TRACED} $(( SPANS / 10 )) $(( SPANS / 2 )) 250000 5"
[[ "$got" == "$want" ]] && pass "Athena obs.traces: ${SPANS} spans; trace id, status, events and duration as sent" \
  || fail "Athena obs.traces got [${got}] want [${want}]"
got="$(athena "SELECT metric_type, count(*), sum(coalesce(cardinality(bucket_counts), 0)),
  round(sum(coalesce(value, 0)), 2) FROM obs.metrics WHERE tenant = '${T}' AND dt IN (${DAYS})
  GROUP BY 1 ORDER BY 1" | norm)" || fail "Athena metrics query"
want="gauge ${GAUGE} 0 49.5 histogram ${HIST} $(( HIST * 3 )) 0.0 sum ${SUM} 0 1225.0"
[[ "$got" == "$want" ]] && pass "Athena obs.metrics: gauge, sum and histogram points and buckets as sent" \
  || fail "Athena obs.metrics got [${got}] want [${want}]"

exit $FAILED
