#!/usr/bin/env bash
# Tests the customer's CloudWatch Logs setup stack (examples/aws/cloudwatch-logs.yaml) for real, as a
# customer would run it, in about 10 minutes. Needs AWS credentials that can create IAM roles, Lambda,
# Firehose, S3 and CloudWatch Logs subscriptions (CloudShell as an admin), in Leasyd's account and region.
#
#   bash infra/cloudwatch-logs-test.sh
#
# 1. A throwaway qa- tenant and its API key.
# 2. A small Lambda function ("the customer's app") with its log group.
# 3. The setup stack, downloaded from the app as Get started shows it, sending that log group
#    to https://ingest.leasyd.com/v1/aws/cloudwatch-logs.
# 4. The function runs a few times; its logs must show up in Leasyd: under the function's name,
#    with ERROR / WARN severity, the trace id, and the REPORT line's duration.
# 5. Everything is deleted at the end, whatever happened (stacks, bucket, tenant).
set -euo pipefail
: "${AWS_DEFAULT_REGION:=us-east-1}"; export AWS_DEFAULT_REGION
RUN="$(date +%m%d%H%M)"
TENANT="qa-cwstack${RUN}"
FN="qa-cwlogs-fn-${RUN}"
APP_STACK="qa-cwlogs-app-${RUN}"
SETUP_STACK="qa-cwlogs-setup-${RUN}"
TEMPLATE_URL="${TEMPLATE_URL:-https://app.leasyd.com/install/cloudwatch-logs.yaml}"
TRACE="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
WORK="$(mktemp -d)"
CBF=(); aws --version 2>&1 | grep -q "aws-cli/2" && CBF=(--cli-binary-format raw-in-base64-out)
say() { echo "$(date +%H:%M:%S) $*"; }
invoke() { aws lambda invoke --function-name "$1" "${CBF[@]}" --payload "$2" "$WORK/out.json" >/dev/null; cat "$WORK/out.json"; }

cleanup() {
  set +e
  say "cleanup"
  bucket="$(aws cloudformation describe-stacks --stack-name "$SETUP_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='FailedDataBucket'].OutputValue" --output text 2>/dev/null)"
  if [[ -n "$bucket" && "$bucket" != None ]]; then
    n="$(aws s3 ls "s3://$bucket" --recursive | wc -l)"
    echo "  data Leasyd refused (failed-data bucket): $n object(s)"
    aws s3 rm "s3://$bucket" --recursive --quiet
  fi
  aws cloudformation delete-stack --stack-name "$SETUP_STACK"
  aws cloudformation delete-stack --stack-name "$APP_STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$SETUP_STACK" && echo "  setup stack deleted"
  aws cloudformation wait stack-delete-complete --stack-name "$APP_STACK" && echo "  app stack deleted"
  [[ "$TENANT" == qa-* ]] && invoke obs-tenant-admin "{\"action\":\"delete\",\"tenant\":\"$TENANT\"}" >/dev/null && echo "  tenant $TENANT deleted"
  rm -rf "$WORK"
}
trap cleanup EXIT

say "1. tenant $TENANT"
KEY="$(invoke obs-tenant-admin "{\"action\":\"create\",\"tenant\":\"$TENANT\",\"plan\":\"standard\"}" \
  | python3 -c 'import json,sys; o=json.load(sys.stdin); print(o.get("api_key") or sys.exit("create failed: %s" % o))')"

say "2. the customer's app: Lambda function $FN"
cat > "$WORK/app.yaml" <<YAML
Resources:
  Logs:
    Type: AWS::Logs::LogGroup
    Properties: { LogGroupName: /aws/lambda/$FN, RetentionInDays: 1 }
  Role:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Statement: [{ Effect: Allow, Principal: { Service: lambda.amazonaws.com }, Action: sts:AssumeRole }]
      ManagedPolicyArns: [arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]
  Fn:
    Type: AWS::Lambda::Function
    DependsOn: Logs
    Properties:
      FunctionName: $FN
      Runtime: python3.12
      Handler: index.handler
      Role: !GetAtt Role.Arn
      Code:
        ZipFile: |
          import json
          def handler(event, context):
              print(json.dumps({"level": "error", "message": "qa payment failed $RUN", "trace_id": "$TRACE", "order": 42}))
              print("WARN: qa slow bank $RUN")
              return "ok"
YAML
aws cloudformation deploy --stack-name "$APP_STACK" --template-file "$WORK/app.yaml" --capabilities CAPABILITY_IAM >/dev/null

say "3. the setup stack, from $TEMPLATE_URL"
curl -fsSL -o "$WORK/leasyd-cloudwatch-logs.yaml" "$TEMPLATE_URL"
aws cloudformation deploy --stack-name "$SETUP_STACK" --template-file "$WORK/leasyd-cloudwatch-logs.yaml" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides LeasydApiKey="$KEY" LogGroups="/aws/lambda/$FN" >/dev/null
aws logs describe-subscription-filters --log-group-name "/aws/lambda/$FN" \
  --query "subscriptionFilters[].[filterName,destinationArn]" --output text | sed 's/^/  filter: /'

say "4. run the function (the new key works within about 2 minutes, Firehose retries until then)"
for i in 1 2 3; do
  aws lambda invoke --function-name "$FN" "$WORK/fn.json" >/dev/null && echo "  run $i: $(cat "$WORK/fn.json")"
  sleep 20
done

say "   waiting for the logs in Leasyd (up to 8 minutes)"
python3 - "$TENANT" "$FN" "$RUN" "$TRACE" "${CBF[@]}" <<'PY'
import json, subprocess, sys, time
tenant, fn, run, trace, cbf = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]

def query(body):
    now = time.time()
    body.update({"tenant": tenant, "signal": "logs", "services": [fn],
                 "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),
                 "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 600))})
    subprocess.run(["aws", "lambda", "invoke", "--function-name", "obs-query", *cbf, "--payload", json.dumps(body),
                    "/tmp/cwq.json"], check=True, capture_output=True)
    return json.load(open("/tmp/cwq.json"))

t0 = time.time()
while True:
    q = query({"search": {"limit": 50}})
    rows = [dict(zip(q.get("columns", []), r)) for r in q.get("rows", [])]
    err = [r for r in rows if f"qa payment failed {run}" in str(r.get("body"))]
    warn = [r for r in rows if f"qa slow bank {run}" in str(r.get("body"))]
    report = [r for r in rows if str(r.get("body")).startswith("REPORT")]
    if (err and warn and report) or time.time() - t0 > 480:
        break
    time.sleep(30)
print(f"  after {int(time.time() - t0)} s: {len(rows)} logs under service {fn}: "
      f"{len(err)} error, {len(warn)} warning, {len(report)} REPORT lines")
ok = True
def check(what, cond):
    global ok
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    ok &= bool(cond)
check("logs arrived under the function's name", rows)
check("JSON error log: severity ERROR", err and err[0].get("severity_text") == "ERROR")
check("JSON error log: trace id kept", err and str(err[0].get("trace_id") or "").lower() == trace)
check("text warning: severity WARN", warn and warn[0].get("severity_text") == "WARN")
if report:
    r = query({"group_by": ["attributes.faas.coldstart"],
               "aggs": [{"fn": "count"}, {"fn": "max", "field": "attributes.aws.lambda.duration_ms"}],
               "where": [{"field": "attributes.aws.lambda.platform", "op": "=", "value": "report"}]})
    print(f"  REPORT lines by cold start (count, max duration ms): {r.get('rows')}")
    check("REPORT duration queryable", r.get("rows"))
else:
    check("REPORT lines arrived", False)
print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
PY
