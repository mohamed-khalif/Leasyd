#!/usr/bin/env bash
# Runs the sample Ruby service with OpenTelemetry, sending straight to Leasyd.
#
#   LEASYD_API_KEY=obs_... bash run.sh
#
# Needs Ruby 3.1+ and Bundler. For your own app: the gems in the Gemfile, the OpenTelemetry setup
# at the top of app.rb, leasyd_runtime_metrics.rb, and the OTEL_* settings below.
set -euo pipefail
: "${LEASYD_API_KEY:?set LEASYD_API_KEY to an API key that sends data (Leasyd > Settings > API keys)}"
cd "$(dirname "$0")"
export BUNDLE_PATH=vendor/bundle
bundle check >/dev/null 2>&1 || bundle install --quiet

export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-orders-ruby}"
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=${ENVIRONMENT:-dev},service.version=1.0.0"
export OTEL_EXPORTER_OTLP_ENDPOINT="${LEASYD_ENDPOINT:-https://ingest.leasyd.com}"
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=${LEASYD_API_KEY}"
export OTEL_METRIC_EXPORT_INTERVAL=30000        # runtime metrics every 30 s
exec bundle exec ruby app.rb
