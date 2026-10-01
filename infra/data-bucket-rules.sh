#!/usr/bin/env bash
# The data bucket's lifecycle rules, for accounts whose bucket isn't in obs-state yet (obs-state
# sets the same rules itself; there this does nothing). Keep in step with infra/state.yaml:
#   retention backstops: data/ and _incoming/ expire after 35 days (the tenant admin's nightly
#     retention job deletes each tenant's data after 30; this only catches what it missed)
#   synthetics/ (browser checks' screenshots) expire after 30 days
#   _results/ (long queries' answers) expire after a day; _ai/ (AI SRE conversations) after 30 days
#   incomplete multipart uploads are aborted after a day
# Replaces the bucket's rules (put-bucket-lifecycle-configuration always does). Safe to repeat.
set -euo pipefail
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
if aws cloudformation describe-stacks --stack-name obs-state >/dev/null 2>&1; then
  exit 0
fi
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
DATA="obs-data-${ACCOUNT}-${AWS_DEFAULT_REGION}"
RULES="$(mktemp)"
cat > "$RULES" <<'JSON'
{"Rules": [
  {"ID": "retention-backstop-data", "Status": "Enabled", "Filter": {"Prefix": "data/"}, "Expiration": {"Days": 35}},
  {"ID": "retention-backstop-incoming", "Status": "Enabled", "Filter": {"Prefix": "_incoming/"}, "Expiration": {"Days": 35}},
  {"ID": "synthetics-screenshots", "Status": "Enabled", "Filter": {"Prefix": "synthetics/"}, "Expiration": {"Days": 30}},
  {"ID": "ai-conversations", "Status": "Enabled", "Filter": {"Prefix": "_ai/"}, "Expiration": {"Days": 30}},
  {"ID": "query-results", "Status": "Enabled", "Filter": {"Prefix": "_results/"}, "Expiration": {"Days": 1}},
  {"ID": "abort-incomplete-uploads", "Status": "Enabled", "Filter": {"Prefix": ""}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}
]}
JSON
aws s3api put-bucket-lifecycle-configuration --bucket "$DATA" --lifecycle-configuration "file://${RULES}"
echo "Data bucket rules set on ${DATA} (35-day retention backstop, 30-day screenshots, 1-day query results, 30-day AI conversations)"
