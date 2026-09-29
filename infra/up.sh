#!/usr/bin/env bash
# Brings the whole platform up in an AWS account, or updates it: every stack in order. Safe to
# re-run; an unchanged stack is left alone. Needs admin credentials (it creates IAM roles and the
# permissions boundary), Node.js 18+ and Python 3 (CloudShell has both).
#
#   infra/up.sh                            deploy or update everything
#   ALERT_EMAIL=you@example.com infra/up.sh   where alarms and budget alerts go (first run; kept after)
#   DOMAIN=leasyd.com infra/up.sh           also the product's domain (see infra/deploy-dns.sh)
#   TEST_TOOLS=1 infra/up.sh                also the load generator and Athena workgroup (dev accounts)
#   DEMO=1 infra/up.sh                      also the "leasyd-demo" tenant's live demo data (obs-phaseD1)
#
# infra/down.sh takes it down again. Tenants, keys, data and logins live in obs-state and survive
# a down/up: the "restore" step below reconnects them to the recreated API.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
step() { echo; echo "=== $* ($(date -u +%H:%M:%S) UTC)" >&2; }
out() { aws cloudformation describe-stacks --stack-name "$1" \
  --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null || true; }
exists() { aws cloudformation describe-stacks --stack-name "$1" >/dev/null 2>&1; }

BUDGET_ARGS=(); ALERT_ARGS=()
if [[ -n "${ALERT_EMAIL:-}" ]]; then
  BUDGET_ARGS=(--parameter-overrides "BudgetEmail=${ALERT_EMAIL}")
  ALERT_ARGS=(--parameter-overrides "AlertEmail=${ALERT_EMAIL}")
fi
echo "Leasyd up: account ${ACCOUNT}, ${AWS_DEFAULT_REGION}" >&2

if [[ -n "${DOMAIN:-}" ]] || exists obs-dns; then
  step "domain (obs-dns)"
  infra/deploy-dns.sh ${DOMAIN:+"$DOMAIN"}
fi

step "data, tenants and logins (obs-state)"
aws cloudformation deploy --stack-name obs-state --template-file infra/state.yaml \
  --tags project=obs phase=state --no-fail-on-empty-changeset

step "roles, boundary, budget (obs-phase0)"
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=0 --no-fail-on-empty-changeset "${BUDGET_ARGS[@]}"

step "compaction and fast lane (obs-phase2)"
infra/deploy-phase2.sh --no-fail-on-empty-changeset "${ALERT_ARGS[@]}"
step "index lookups (obs-phase3)"
infra/deploy-phase3.sh --no-fail-on-empty-changeset
step "query engine and query API (obs-phase4)"
infra/deploy-phase4.sh --no-fail-on-empty-changeset
step "synthetic checks (obs-phaseS1; before the API, whose routes use it)"
infra/deploy-phaseS1.sh --no-fail-on-empty-changeset
step "ingest API (obs-phaseT2)"
infra/deploy-phaseT2.sh --no-fail-on-empty-changeset
step "tenant operations (obs-phaseT5)"
infra/deploy-phaseT5.sh --no-fail-on-empty-changeset

step "reconnect existing tenants to the API"
infra/tenant.sh restore

step "canary and alarms (obs-phaseT7)"
infra/deploy-phaseT7.sh --no-fail-on-empty-changeset
step "web app (obs-phaseW1)"
infra/deploy-phaseW1.sh --no-fail-on-empty-changeset

if [[ -n "${DEMO:-}" ]] || exists obs-phaseD1; then
  step "demo tenant data (obs-phaseD1)"
  infra/deploy-phaseD1.sh --no-fail-on-empty-changeset
fi

if [[ -n "${TEST_TOOLS:-}" ]]; then
  step "test tools: load generator and Athena (obs-phase1, obs-phaseT6)"
  aws cloudformation deploy --stack-name obs-phase1 --template-file infra/phase1-write-path.yaml \
    --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=1 --no-fail-on-empty-changeset
  infra/deploy-phaseT6.sh --no-fail-on-empty-changeset
fi

cat >&2 <<EOF

Leasyd is up.
  Ingest endpoint (for customers): $(out obs-phaseT2 IngestEndpoint)
  Web app:                         $(out obs-phaseW1 WebUrl)
Alarm emails go to the obs-alerts topic: on a first run, confirm the subscription email AWS sends.
EOF
