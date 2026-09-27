#!/usr/bin/env bash
# Runs the collector config embedded in phase1-write-path.yaml, locally, with
# the real collector binary and the real S3 bucket, and checks:
#   1. a request's tenant comes from the x-obs-tenant header, not the client's data
#   2. a request without the header is dropped (a client can't pick a tenant)
#   3. the client is answered only after its batch is in S3 (wait_for_result)
#   4. SIGKILL mid-batch: the client gets an error (so its SDK retries), nothing half-written
#   5. SIGTERM mid-batch: the batch is flushed and the client gets 200
# Writes only under _incoming/tenant=ztest-*/ and deletes it afterwards.
#
# Needs: AWS credentials that can put/delete objects in the data bucket
# (obs-deployer can), python3 with PyYAML, curl.
set -uo pipefail

: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
VERSION=0.161.0
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="obs-data-${ACCOUNT}-${AWS_DEFAULT_REGION}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
FAILED=0
CPID=""
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
cleanup() {
  [[ -n "$CPID" ]] && kill "$CPID" 2>/dev/null
  for t in ztest-a ztest-b ztest-evil ztest-kill ztest-term; do
    aws s3 rm --recursive --quiet "s3://${BUCKET}/_incoming/tenant=${t}/" 2>/dev/null
  done
  aws s3 rm --recursive --quiet "s3://${BUCKET}/_incoming/_untenanted/" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

# ---- collector binary and config ----
case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) echo "unsupported arch"; exit 2 ;; esac
curl -sSLf -o "$WORK/otel.tgz" \
  "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${VERSION}/otelcol-contrib_${VERSION}_linux_${ARCH}.tar.gz" \
  && tar -xzf "$WORK/otel.tgz" -C "$WORK" otelcol-contrib || { echo "could not download the collector"; exit 2; }
python3 - "$HERE/phase1-write-path.yaml" "$WORK/config.yaml" "$BUCKET" "$AWS_DEFAULT_REGION" <<'EOF'
import sys, yaml
class L(yaml.SafeLoader): pass
for t in ["!Sub", "!Ref", "!GetAtt", "!FindInMap", "!Select", "!GetAZs", "!Join", "!If", "!Equals"]:
    L.add_constructor(t, lambda l, n: l.construct_sequence(n) if isinstance(n, yaml.SequenceNode) else l.construct_scalar(n))
tpl = yaml.load(open(sys.argv[1]), Loader=L)
env = tpl["Resources"]["CollectorTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
cfg = next(e["Value"][0] for e in env if e["Name"] == "OTEL_CONFIG")
cfg = (cfg.replace("${AWS::Region}", sys.argv[4]).replace("${Bucket}", sys.argv[3])
          .replace("${FlushTimeout}", "5s").replace("0.0.0.0:", "127.0.0.1:"))
open(sys.argv[2], "w").write(cfg)
EOF
"$WORK/otelcol-contrib" validate --config="$WORK/config.yaml" >/dev/null 2>&1 \
  && pass "config from the template is valid for collector ${VERSION}" \
  || { "$WORK/otelcol-contrib" validate --config="$WORK/config.yaml"; fail "config invalid"; exit 1; }

start() {
  "$WORK/otelcol-contrib" --config="$WORK/config.yaml" >"$WORK/col.log" 2>&1 & CPID=$!
  for _ in $(seq 40); do curl -s 127.0.0.1:13133/ >/dev/null 2>&1 && return; sleep 0.25; done
  echo "collector didn't start:"; cat "$WORK/col.log"; exit 1
}
body() {  # body <spoofed obs.tenant> <message>
  printf '{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"api"}},{"key":"obs.tenant","value":{"stringValue":"%s"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"%s000000000","body":{"stringValue":"%s"}}]}]}]}' "$1" "$(date +%s)" "$2"
}
send() {  # send <tenant header or ""> <spoof> <message> -> "<http code> <seconds> <curl exit>"
  local h=(); [[ -n "$1" ]] && h=(-H "x-obs-tenant: $1")
  local out; out="$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -X POST 127.0.0.1:4318/v1/logs \
    -H 'Content-Type: application/json' "${h[@]}" --data "$(body "$2" "$3")")"; echo "$out $?"
}
count() { aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$1" --query 'length(not_null(Contents, `[]`))' --output text; }
tenant_in_file() {  # obs.tenant recorded in the first object under a prefix
  local k; k="$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$1" --query 'Contents[0].Key' --output text)"
  aws s3 cp "s3://${BUCKET}/${k}" "$WORK/o.gz" >/dev/null 2>&1 && gzip -dc "$WORK/o.gz" | python3 -c \
    "import json,sys; r=json.load(sys.stdin)['resourceLogs'][0]; print({a['key']:a['value'].get('stringValue') for a in r['resource']['attributes']}.get('obs.tenant'))"
}

# ---- 1-3: tenant stamping, spoofing, missing header, ack after write ----
start
send ztest-a ztest-evil from-a > "$WORK/a" &
send ztest-b ""         from-b > "$WORK/b" &
send ""      ztest-evil no-hdr > "$WORK/c" &
wait %2 %3 %4 2>/dev/null; sleep 2
read -r code secs _ < "$WORK/a"
[[ "$code" == 200 ]] && awk "BEGIN{exit !($secs >= 4)}" \
  && pass "client answered only after the batch was written (${secs}s, flush 5s)" \
  || fail "request A: HTTP ${code} after ${secs}s (expected 200 after >= 4s)"
[[ "$(count _incoming/tenant=ztest-a/)" -ge 1 && "$(tenant_in_file _incoming/tenant=ztest-a/)" == ztest-a ]] \
  && pass "spoofed obs.tenant=ztest-evil was overwritten: filed and stamped as ztest-a" \
  || fail "request A not filed as ztest-a"
[[ "$(count _incoming/tenant=ztest-b/)" -ge 1 ]] && pass "ztest-b filed separately" || fail "ztest-b missing"
[[ "$(count _incoming/tenant=ztest-evil/)" == 0 && "$(count _incoming/_untenanted/)" == 0 ]] \
  && pass "request without a tenant header dropped (nothing under ztest-evil or _untenanted)" \
  || fail "request without a tenant header was stored"
kill "$CPID"; wait "$CPID" 2>/dev/null; CPID=""

# ---- 4: SIGKILL mid-batch ----
start
send ztest-kill "" in-flight > "$WORK/k" & sleep 2
kill -9 "$CPID"; wait "$CPID" 2>/dev/null; CPID=""; wait 2>/dev/null; sleep 1
read -r code secs rc < "$WORK/k"
[[ "$code" != 200 && "$(count _incoming/tenant=ztest-kill/)" == 0 ]] \
  && pass "SIGKILL mid-batch: client got no OK (HTTP ${code}, curl exit ${rc}) so its SDK retries" \
  || fail "SIGKILL mid-batch: client got HTTP ${code}; objects: $(count _incoming/tenant=ztest-kill/)"

# ---- 5: SIGTERM mid-batch ----
start
send ztest-term "" in-flight > "$WORK/t" & sleep 2
kill -TERM "$CPID"; wait "$CPID" 2>/dev/null; CPID=""; wait 2>/dev/null; sleep 1
read -r code secs _ < "$WORK/t"
[[ "$code" == 200 && "$(count _incoming/tenant=ztest-term/)" -ge 1 ]] \
  && pass "SIGTERM mid-batch: batch flushed and client got 200 (graceful scale-in)" \
  || fail "SIGTERM mid-batch: HTTP ${code}, objects $(count _incoming/tenant=ztest-term/)"

exit $FAILED
