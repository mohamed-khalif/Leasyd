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

# Run a command under a component role's credentials. TAG=<tenant> adds a session tag.
as_role() {
  local arn="$1"; shift
  local creds tags=()
  [[ -n "${TAG:-}" ]] && tags=(--tags "Key=tenant,Value=${TAG}")
  creds="$(aws sts assume-role --role-arn "$arn" --role-session-name phase0-test "${tags[@]}" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text 2>&1)" \
    || { echo "$creds"; return 99; }
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
READER="$(output TenantReaderRoleArn)"
KEY="dt=2026-01-01/hour=00/service=phase0-test"
IN="_incoming/tenant=acme/logs/$KEY"
A="data/tenant=acme/logs/$KEY"      # tenant under test
G="data/tenant=globex/logs/$KEY"    # another tenant

expect allow "collector writes _incoming/"        "$COLLECTOR" aws s3api put-object --bucket "$BUCKET" --key "$IN/a.json" --body "$TMP/small.txt"
expect deny  "collector reads _incoming/"         "$COLLECTOR" aws s3api get-object --bucket "$BUCKET" --key "$IN/a.json" "$TMP/out"
expect deny  "collector writes data/"             "$COLLECTOR" aws s3api put-object --bucket "$BUCKET" --key "$A/a.parquet" --body "$TMP/small.txt"
expect deny  "collector lists bucket"             "$COLLECTOR" aws s3api list-objects-v2 --bucket "$BUCKET" --max-items 1

expect allow "compaction reads _incoming/"        "$COMPACTION" aws s3api get-object --bucket "$BUCKET" --key "$IN/a.json" "$TMP/out"
expect allow "compaction writes data/ (acme)"     "$COMPACTION" aws s3api put-object --bucket "$BUCKET" --key "$A/a.parquet" --body "$TMP/small.txt"
expect allow "compaction writes data/ (globex)"   "$COMPACTION" aws s3api put-object --bucket "$BUCKET" --key "$G/a.parquet" --body "$TMP/small.txt"
expect deny  "compaction writes _incoming/"       "$COMPACTION" aws s3api put-object --bucket "$BUCKET" --key "$IN/b.json" --body "$TMP/small.txt"
expect deny  "compaction writes _results/"        "$COMPACTION" aws s3api put-object --bucket "$BUCKET" --key "_results/x" --body "$TMP/small.txt"
expect allow "compaction deletes _incoming/"      "$COMPACTION" aws s3api delete-object --bucket "$BUCKET" --key "$IN/a.json"
for t in acme globex; do  # index entries for the reader checks
  expect allow "compaction writes index (${t})"   "$COMPACTION" aws dynamodb put-item --table-name obs-index \
    --item "{\"pk\":{\"S\":\"${t}#logs#phase0-test\"},\"sk\":{\"S\":\"x\"}}"
done

# The query role itself reaches no tenant data: it must go through obs-tenant-reader.
expect deny  "query reads data/ directly"         "$QUERY" aws s3api get-object --bucket "$BUCKET" --key "$A/a.parquet" "$TMP/out"
expect deny  "query reads the index directly"     "$QUERY" aws dynamodb query --table-name obs-index \
  --key-condition-expression 'pk = :p' --expression-attribute-values '{":p":{"S":"acme#logs#phase0-test"}}'
expect deny  "query writes _incoming/"            "$QUERY" aws s3api put-object --bucket "$BUCKET" --key "$IN/c.json" --body "$TMP/small.txt"

# obs-tenant-reader, tagged tenant=acme: acme's data and index only.
export TAG=acme
expect allow "reader(acme) reads acme data"       "$READER" aws s3api get-object --bucket "$BUCKET" --key "$A/a.parquet" "$TMP/out"
expect deny  "reader(acme) reads globex data"     "$READER" aws s3api get-object --bucket "$BUCKET" --key "$G/a.parquet" "$TMP/out"
expect allow "reader(acme) lists acme data"       "$READER" aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "data/tenant=acme/" --max-items 1
expect deny  "reader(acme) lists globex data"     "$READER" aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "data/tenant=globex/" --max-items 1
expect deny  "reader(acme) lists whole bucket"    "$READER" aws s3api list-objects-v2 --bucket "$BUCKET" --max-items 1
expect allow "reader(acme) queries acme index"    "$READER" aws dynamodb query --table-name obs-index \
  --key-condition-expression 'pk = :p' --expression-attribute-values '{":p":{"S":"acme#logs#phase0-test"}}'
expect deny  "reader(acme) queries globex index"  "$READER" aws dynamodb query --table-name obs-index \
  --key-condition-expression 'pk = :p' --expression-attribute-values '{":p":{"S":"globex#logs#phase0-test"}}'
expect deny  "reader(acme) reads internal plans"  "$READER" aws dynamodb query --table-name obs-index \
  --key-condition-expression 'pk = :p' --expression-attribute-values '{":p":{"S":"_plan#acme#logs#2026-01-01#00"}}'
expect deny  "reader(acme) writes acme data"      "$READER" aws s3api put-object --bucket "$BUCKET" --key "$A/b.parquet" --body "$TMP/small.txt"
unset TAG
expect deny  "reader without a tenant tag"        "$READER" aws s3api get-object --bucket "$BUCKET" --key "$A/a.parquet" "$TMP/out"

# Clean up: objects with the caller's own credentials, index entries with the compaction role.
aws s3api delete-object --bucket "$BUCKET" --key "$A/a.parquet" >/dev/null 2>&1
aws s3api delete-object --bucket "$BUCKET" --key "$G/a.parquet" >/dev/null 2>&1
for t in acme globex; do
  as_role "$COMPACTION" aws dynamodb delete-item --table-name obs-index \
    --key "{\"pk\":{\"S\":\"${t}#logs#phase0-test\"},\"sk\":{\"S\":\"x\"}}" >/dev/null 2>&1
done

# Lifecycle probe. S3 skips transitioning objects under 128 KB, so make it 256 KB.
# Only uploaded once: re-uploading would restart the transition clock.
if aws s3api head-object --bucket "$BUCKET" --key "$PROBE_KEY" >/dev/null 2>&1; then
  echo "INFO  lifecycle probe already present; check it with '$0 $STACK lifecycle'"
else
  head -c 262144 /dev/urandom > "$TMP/probe.bin"
  aws s3api put-object --bucket "$BUCKET" --key "$PROBE_KEY" --body "$TMP/probe.bin" >/dev/null \
    && echo "Uploaded lifecycle probe s3://$BUCKET/$PROBE_KEY; re-run with 'lifecycle' in 1-2 days."
fi

exit $FAILED
