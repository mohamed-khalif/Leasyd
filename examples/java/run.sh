#!/usr/bin/env bash
# Runs PaymentsService with the OpenTelemetry Java agent, sending straight to Leasyd.
#
#   LEASYD_API_KEY=obs_... bash run.sh
#
# Needs Java 17+ (javac). For your own app, keep the -javaagent line and the OTEL_* settings.
set -euo pipefail
: "${LEASYD_API_KEY:?set LEASYD_API_KEY to an API key that sends data (Leasyd > Settings > API keys)}"
cd "$(dirname "$0")"
AGENT_VERSION="${AGENT_VERSION:-2.31.1}"
[[ -f opentelemetry-javaagent.jar ]] || curl -fsSL -o opentelemetry-javaagent.jar \
  "https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/download/v${AGENT_VERSION}/opentelemetry-javaagent.jar"
javac -d build PaymentsService.java

export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-payments-service}"
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=${ENVIRONMENT:-dev},service.version=1.0.0"
export OTEL_EXPORTER_OTLP_ENDPOINT="${LEASYD_ENDPOINT:-https://ingest.leasyd.com}"
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=${LEASYD_API_KEY}"
export OTEL_METRIC_EXPORT_INTERVAL=30000        # JVM metrics every 30 s
exec java -javaagent:opentelemetry-javaagent.jar -Xmx256m -cp build PaymentsService
