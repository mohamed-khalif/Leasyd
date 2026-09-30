#!/usr/bin/env bash
# Takes the platform down. Needs admin credentials.
#
#   infra/down.sh                 deletes the compute stacks (API, ingest, compaction, queries, web app,
#                                 canary, roles). Kept: customer data, tenants, API keys, logins
#                                 (obs-state) and the domain (obs-dns). infra/up.sh brings it back
#                                 with everything reconnected; customers' keys keep working.
#   infra/down.sh --delete-data   also deletes every tenant's data, streams and API keys, all logins
#                                 and obs-state itself. Cannot be undone.
#
# The domain's zone (obs-dns) is never deleted: its name servers would change, and the registrar
# would need updating again. Delete that stack by hand if the domain is no longer wanted.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGION="$AWS_DEFAULT_REGION"
DELETE_DATA=""
[[ "${1:-}" == "--delete-data" ]] && DELETE_DATA=1
[[ $# -gt 1 || ( $# -eq 1 && -z "$DELETE_DATA" ) ]] && { sed -n '2,13p' "$0"; exit 2; }

step() { echo; echo "=== $* ($(date -u +%H:%M:%S) UTC)" >&2; }
exists() { aws cloudformation describe-stacks --stack-name "$1" >/dev/null 2>&1; }
empty_bucket() {  # buckets CloudFormation must delete have to be empty first
  if aws s3api head-bucket --bucket "$1" 2>/dev/null; then
    aws s3 rm "s3://$1" --recursive --only-show-errors
  fi
}
names() {  # names <aws command with --query>: the listed names, one per word ("None" = none)
  "$@" --output text | tr '\t' '\n' | grep -v '^None$' || true
}
delete_stack() {
  exists "$1" || return 0
  step "deleting $1"
  aws cloudformation delete-stack --stack-name "$1"
  aws cloudformation wait stack-delete-complete --stack-name "$1"
}

# ---- confirm
if [[ -n "$DELETE_DATA" ]]; then
  echo "This DELETES ALL CUSTOMER DATA, tenants, API keys and logins in account ${ACCOUNT} (${REGION})." >&2
  echo "It cannot be undone." >&2
else
  echo "This takes Leasyd offline in account ${ACCOUNT} (${REGION}). Data, tenants, keys and logins are kept." >&2
fi
if [[ "${CONFIRM:-}" != "$ACCOUNT" ]]; then
  read -r -p "Type the account id (${ACCOUNT}) to continue: " answer
  [[ "$answer" == "$ACCOUNT" ]] || { echo "not confirmed; nothing changed" >&2; exit 1; }
fi

# ---- compute stacks, newest first (each depends only on stacks deleted after it)
empty_bucket "obs-web-${ACCOUNT}-${REGION}"
delete_stack obs-phaseT6          # test tools
delete_stack obs-phaseD1          # demo tenant data
delete_stack obs-phaseW1          # web app (CloudFront takes 5-15 minutes)
delete_stack obs-phaseT7
delete_stack obs-phaseT5
delete_stack obs-phaseT2
delete_stack obs-phaseA1          # alerts (rules and channels stay in obs-tenants; email topics stay)
delete_stack obs-phaseS1          # synthetic checks (their settings stay in obs-tenants)
delete_stack obs-phaseS1-build    # the browser image (rebuilt by up.sh)
delete_stack obs-phase4
delete_stack obs-phase3
empty_bucket "obs-athena-results-${ACCOUNT}-${REGION}"
delete_stack obs-phase1           # test tools; its Athena table is in phase 2's database
delete_stack obs-phase2
empty_bucket "obs-artifacts-${ACCOUNT}-${REGION}"
delete_stack obs-phase0

if [[ -z "$DELETE_DATA" ]]; then
  cat >&2 <<EOF

Leasyd is down. Kept: obs-state (data, tenants, keys, logins)$(exists obs-dns && echo " and obs-dns (domain)").
Tenants' Firehose streams are kept too (they cost nothing idle). infra/up.sh brings everything back.
EOF
  exit 0
fi

# ---- everything that holds data (all retained by their stacks, so deleted here by name)
step "deleting tenants' ingest streams"
for s in $(names aws firehose list-delivery-streams --limit 10000 \
             --query "DeliveryStreamNames[?starts_with(@, 'obs-t-')]"); do
  aws firehose delete-delivery-stream --delivery-stream-name "$s" && echo "  $s"
done

step "deleting tenants' API keys"
for k in $(names aws apigateway get-api-keys --no-include-values --query "items[?tags.project=='obs'].id"); do
  aws apigateway delete-api-key --api-key "$k"
done

step "deleting platform secrets (SSM /obs/)"
for p in $(names aws ssm get-parameters-by-path --path /obs --recursive --query "Parameters[].Name"); do
  aws ssm delete-parameter --name "$p"
done

delete_stack obs-state
delete_stack obs-phaseU1          # logins stack of earlier versions

step "deleting the data bucket, tables and user pool"
BUCKET="obs-data-${ACCOUNT}-${REGION}"
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3 rm "s3://${BUCKET}" --recursive --only-show-errors
  aws s3api delete-bucket --bucket "$BUCKET"
fi
for t in obs-index obs-usage obs-tenants; do
  aws dynamodb delete-table --table-name "$t" >/dev/null 2>&1 && echo "  table $t" || true
done
for p in $(names aws cognito-idp list-user-pools --max-results 60 --query "UserPools[?Name=='obs-users'].Id"); do
  aws cognito-idp delete-user-pool --user-pool-id "$p" && echo "  pool $p"
done

step "deleting leftover Lambda log groups"
for g in $(names aws logs describe-log-groups --log-group-name-prefix /aws/lambda/obs- \
             --query "logGroups[].logGroupName"); do
  aws logs delete-log-group --log-group-name "$g"
done

echo >&2
echo "Leasyd and all its data are deleted$(exists obs-dns && echo "; the domain zone (obs-dns) is kept")." >&2
