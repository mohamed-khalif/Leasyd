# Node.js service with OpenTelemetry auto-instrumentation

`server.js` is a tiny Node.js web service (no frameworks) that calls itself a few times a second
and now and then blocks its event loop with a slow report. `run.sh` starts it with OpenTelemetry's
auto-instrumentation, which sends its traces and runtime metrics (event loop delay and
utilization, V8 heap, garbage collection, active handles) straight to Leasyd.

    LEASYD_API_KEY=obs_... bash run.sh          # needs Node.js 18+

In Leasyd: **Service Map** > `inventory-api` > **Node.js dashboard**, or **Dashboards** > *Leasyd - Node.js*.

Your own app: `npm install @opentelemetry/api @opentelemetry/auto-instrumentations-node`, then start
it with `--require @opentelemetry/auto-instrumentations-node/register` and the `OTEL_*` settings from
`run.sh`. Behind the EC2 collector (`examples/ec2/setup.sh`), use
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` and no API key header.
