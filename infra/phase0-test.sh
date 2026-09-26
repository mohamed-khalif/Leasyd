#!/usr/bin/env bash
# Phase 0 tests: each component role can do its job and is denied
# everything out of scope. Also uploads the lifecycle probe object.
#
# Usage: infra/phase0-test.sh [stack-name]            run IAM tests + upload probe
#        infra/phase0-test.sh [stack-name] lifecycle  check the probe's storage class
set -uo pipefail

STACK="${1:-obs-phase0}"
MODE="${2:-iam}"
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET="$(output BucketName)"
PROBE_KEY="_lifecycle-test/probe.bin"

if [[ "$MODE" == "lifecycle" ]]; then
  # Lifecycle runs roughly once a day; expect GLACIER_IR 1-2 days after upload.
  aws s3api head-object --bucket "$BUCKET" --key "$PROBE_KEY" \
    --query '{StorageClass: StorageClass, LastModified: LastModified}'
  exit 0
fi

FAILED=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "probe" > "$TMP/small.txt"

# Run a command under a component role's credentials.
as_role() {
  local arn="$1"; shift
  local creds
  creds="$(aws sts assume-role --role-arn "$arn" --role-session-name phase0-test \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)" || return 99
  read -r AK SK ST <<<"$creds"
  AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST" "$@"
}

expect() {
  local want="$1" desc="$2" arn="$3"; shift 3
  local out rc
  out="$(as_role "$arn" "$@" 2>&1)"; rc=$?
  if [[ "$want" == allow && $rc -eq 0 ]] || [[ "$want" == deny && "$out" == *AccessDenied* ]]; then
    echo "PASS  $desc"
  else
    echo "FAIL  $desc (expected $want, exit $rc): ${out:0:200}"
    FAILED=1
  fi
}

COLLECTOR="$(output CollectorRoleArn)"
COMPACTION="$(output CompactionRoleArn)"
QUERY="$(output QueryRoleArn)"
KEY="dt=2026-01-01/hour=00/service=phase0-test"

expect allow "collector writes _incoming/"        "$COLLECTOR" aws s3api put-object --bucket "$BUCKET" --key "_incoming/logs/$KEY/a.json" --body "$TMP/small.txt"
expect deny  "collector reads _incoming/"         "$COLLECTOR" aws s3api get-object --bucket "$BUCKET" --key "_incoming/logs/$KEY/a.json" "$TMP/out"
expect deny  "collector writes logs/"             "$COLLECTOR" aws s3api put-object --bucket "$BUCKET" --key "logs/$KEY/a.parquet" --body "$TMP/small.txt"
expect deny  "collector lists bucket"             "$COLLECTOR" aws s3api list-objects-v2 --bucket "$BUCKET" --max-items 1

expect allow "compaction reads _incoming/"        "$COMPACTION" aws s3api get-object --bucket "$BUCKET" --key "_incoming/logs/$KEY/a.json" "$TMP/out"
expect allow "compaction writes logs/"            "$COMPACTION" aws s3api put-object --bucket "$BUCKET" --key "logs/$KEY/a.parquet" --body "$TMP/small.txt"
expect deny  "compaction writes _incoming/"       "$COMPACTION" aws s3api put-object --bucket "$BUCKET" --key "_incoming/logs/$KEY/b.json" --body "$TMP/small.txt"
expect deny  "compaction writes _results/"        "$COMPACTION" aws s3api put-object --bucket "$BUCKET" --key "_results/x" --body "$TMP/small.txt"
expect allow "compaction deletes _incoming/"      "$COMPACTION" aws s3api delete-object --bucket "$BUCKET" --key "_incoming/logs/$KEY/a.json"

expect allow "query reads logs/"                  "$QUERY" aws s3api get-object --bucket "$BUCKET" --key "logs/$KEY/a.parquet" "$TMP/out"
expect deny  "query writes logs/"                 "$QUERY" aws s3api put-object --bucket "$BUCKET" --key "logs/$KEY/b.parquet" --body "$TMP/small.txt"
expect deny  "query deletes logs/"                "$QUERY" aws s3api delete-object --bucket "$BUCKET" --key "logs/$KEY/a.parquet"
expect deny  "query writes _incoming/"            "$QUERY" aws s3api put-object --bucket "$BUCKET" --key "_incoming/logs/$KEY/c.json" --body "$TMP/small.txt"

# Clean up the test object with the caller's own credentials.
aws s3api delete-object --bucket "$BUCKET" --key "logs/$KEY/a.parquet" >/dev/null

# Lifecycle probe. S3 skips transitioning objects under 128 KB, so make it 256 KB.
head -c 262144 /dev/urandom > "$TMP/probe.bin"
aws s3api put-object --bucket "$BUCKET" --key "$PROBE_KEY" --body "$TMP/probe.bin" >/dev/null \
  && echo "Uploaded lifecycle probe s3://$BUCKET/$PROBE_KEY; re-run with 'lifecycle' in 1-2 days."

exit $FAILED
