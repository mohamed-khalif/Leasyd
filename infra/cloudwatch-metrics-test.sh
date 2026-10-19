#!/usr/bin/env bash
# Tests the customer's CloudWatch metrics setup stack (examples/aws/cloudwatch-metrics.yaml) for real, as a
# customer would run it, in about 12 minutes. Needs AWS credentials that can create IAM roles, Lambda,
# Firehose, S3 and CloudWatch metric streams (CloudShell as an admin), in Leasyd's account and region.
#
#   bash infra/cloudwatch-metrics-test.sh
#   TEMPLATE_URL="file://$PWD/examples/aws/cloudwatch-metrics.yaml" bash infra/cloudwatch-metrics-test.sh
#
# 1. A throwaway qa- tenant and its API key.
# 2. A small Lambda function ("the customer's app").
# 3. The setup stack, downloaded from the app as Get started shows it: a metric stream of the region's
#    AWS/Lambda metrics to https://ingest.leasyd.com/v1/aws/cloudwatch-metrics. (It streams every
#    function in the region, Leasyd's own included, into the test tenant for these few minutes.)
# 4. The function runs every 30 s; its metrics must show up in Leasyd under the function's name:
#    aws.lambda.invocations and aws.lambda.duration.
# 5. Everything is deleted at the end, whatever happened (stacks, bucket, tenant).
set -euo pipefail
: "${AWS_DEFAULT_REGION:=us-east-1}"; export AWS_DEFAULT_REGION
RUN="$(date +%m%d%H%M)"
TENANT="qa-cwmetrics${RUN}"
FN="qa-cwmetrics-fn-${RUN}"
APP_STACK="qa-cwmetrics-app-${RUN}"
SETUP_STACK="qa-cwmetrics-setup-${RUN}"
TEMPLATE_URL="${TEMPLATE_URL:-https://app.leasyd.com/install/cloudwatch-metrics.yaml}"
WORK="$(mktemp -d)"
CBF=(); aws --version 2>&1 | grep -q "aws-cli/2" && CBF=(--cli-binary-format raw-in-base64-out)
say() { echo "$(date +%H:%M:%S) $*"; }
invoke() { aws lambda invoke --function-name "$1" "${CBF[@]}" --payload "$2" "$WORK/out.json" >/dev/null; cat "$WORK/out.json"; }
RUNNER=""

cleanup() {
  set +e
  say "cleanup"
  [[ -n "$RUNNER" ]] && kill "$RUNNER" 2>/dev/null
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
          import time
          def handler(event, context):
              time.sleep(0.2)
              return "ok"
YAML
aws cloudformation deploy --stack-name "$APP_STACK" --template-file "$WORK/app.yaml" --capabilities CAPABILITY_IAM >/dev/null

say "3. the setup stack, from $TEMPLATE_URL"
curl -fsSL -o "$WORK/leasyd-cloudwatch-metrics.yaml" "$TEMPLATE_URL"
aws cloudformation deploy --stack-name "$SETUP_STACK" --template-file "$WORK/leasyd-cloudwatch-metrics.yaml" \
  --capabilities CAPABILITY_IAM --parameter-overrides LeasydApiKey="$KEY" >/dev/null
aws cloudformation describe-stacks --stack-name "$SETUP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='MetricStream'].OutputValue" --output text | sed 's/^/  metric stream: /'

say "4. run the function every 30 s while we wait (the stream starts within a few minutes)"
( while true; do aws lambda invoke --function-name "$FN" "$WORK/fn.json" >/dev/null 2>&1; sleep 30; done ) &
RUNNER=$!

say "   waiting for the function's metrics in Leasyd (up to 12 minutes)"
python3 - "$TENANT" "$FN" "${CBF[@]}" <<'PY'
import json, subprocess, sys, time
tenant, fn, cbf = sys.argv[1], sys.argv[2], sys.argv[3:]

def query(body):
    now = time.time()
    body.update({"tenant": tenant, "signal": "metrics",
                 "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),
                 "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 600))})
    subprocess.run(["aws", "lambda", "invoke", "--function-name", "obs-query", *cbf, "--payload", json.dumps(body),
                    "/tmp/cwm.json"], check=True, capture_output=True)
    return json.load(open("/tmp/cwm.json"))

t0, got = time.time(), {}
while True:
    q = query({"services": [fn], "group_by": ["metric_name"], "aggs": [{"fn": "count"}, {"fn": "sum", "field": "sum"}]})
    got = {r[0]: r[1:] for r in q.get("rows", [])}
    if {"aws.lambda.invocations", "aws.lambda.duration"} <= set(got) or time.time() - t0 > 720:
        break
    time.sleep(30)
print(f"  after {int(time.time() - t0)} s, metrics under service {fn} (metric: points, sum):")
for name, v in sorted(got.items()):
    print(f"    {name}: {v}")
others = query({"group_by": ["service"], "aggs": [{"fn": "count"}]})
print(f"  services in the tenant (the region's functions): {len(others.get('rows', []))}")
ok = True
def check(what, cond):
    global ok
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    ok &= bool(cond)
check("aws.lambda.invocations under the function's name", "aws.lambda.invocations" in got)
check("aws.lambda.duration under the function's name", "aws.lambda.duration" in got)
check("invocations counted (sum > 0)", got.get("aws.lambda.invocations") and float(got["aws.lambda.invocations"][1] or 0) > 0)
print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
PY
