#!/usr/bin/env bash
# Runs the sample Python service with OpenTelemetry auto-instrumentation, sending straight to Leasyd.
#
#   LEASYD_API_KEY=obs_... bash run.sh
#
# Needs Python 3.9+. For your own app: pip install opentelemetry-distro opentelemetry-exporter-otlp-proto-http,
# run opentelemetry-bootstrap -a install, then start it with opentelemetry-instrument and the OTEL_* settings below.
set -euo pipefail
: "${LEASYD_API_KEY:?set LEASYD_API_KEY to an API key that sends data (Leasyd > Settings > API keys)}"
cd "$(dirname "$0")"
if [[ ! -d venv ]]; then
  python3 -m venv venv
  venv/bin/pip install -q flask requests opentelemetry-distro opentelemetry-exporter-otlp-proto-http \
    opentelemetry-instrumentation-system-metrics
  venv/bin/opentelemetry-bootstrap -a install >/dev/null
fi

export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-pricing-api}"
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=${ENVIRONMENT:-dev},service.version=1.0.0"
export OTEL_EXPORTER_OTLP_ENDPOINT="${LEASYD_ENDPOINT:-https://ingest.leasyd.com}"
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=${LEASYD_API_KEY}"
export OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
export OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true
export OTEL_METRIC_EXPORT_INTERVAL=30000        # runtime metrics every 30 s
exec venv/bin/opentelemetry-instrument venv/bin/python app.py
