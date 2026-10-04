#!/usr/bin/env bash
# Runs the sample Node.js service with OpenTelemetry auto-instrumentation, sending straight to Leasyd.
#
#   LEASYD_API_KEY=obs_... bash run.sh
#
# Needs Node.js 18+. For your own app: npm install @opentelemetry/api @opentelemetry/auto-instrumentations-node,
# then start it with the --require line and the OTEL_* settings below.
set -euo pipefail
: "${LEASYD_API_KEY:?set LEASYD_API_KEY to an API key that sends data (Leasyd > Settings > API keys)}"
cd "$(dirname "$0")"
[[ -d node_modules ]] || npm install --no-audit --no-fund --silent

export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-inventory-api}"
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=${ENVIRONMENT:-dev},service.version=1.0.0"
export OTEL_EXPORTER_OTLP_ENDPOINT="${LEASYD_ENDPOINT:-https://ingest.leasyd.com}"
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=${LEASYD_API_KEY}"
export OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
export OTEL_METRIC_EXPORT_INTERVAL=30000        # runtime metrics every 30 s
export OTEL_NODE_RESOURCE_DETECTORS="${OTEL_NODE_RESOURCE_DETECTORS:-env,host,os,process,container}"   # add aws on AWS
exec node --require @opentelemetry/auto-instrumentations-node/register server.js
