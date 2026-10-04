# Python service with OpenTelemetry auto-instrumentation

`app.py` is a small Flask service that calls itself a few times a second. `run.sh` starts it
with OpenTelemetry's auto-instrumentation, which sends its traces, logs and runtime metrics
(CPU, memory, garbage collection by generation, threads, open files, context switches) straight
to Leasyd.

    LEASYD_API_KEY=obs_... bash run.sh          # needs Python 3.9+

In Leasyd: **Service Map** > `pricing-api` > **Python dashboard**, or **Dashboards** > *Leasyd - Python*.

Your own app: `pip install opentelemetry-distro opentelemetry-exporter-otlp-proto-http
opentelemetry-instrumentation-system-metrics`, run `opentelemetry-bootstrap -a install`, then start
it with `opentelemetry-instrument` and the `OTEL_*` settings from `run.sh`.
