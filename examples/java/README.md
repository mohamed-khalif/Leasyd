# Java service with the OpenTelemetry Java agent

`PaymentsService` is a tiny Java web service (plain JDK) that calls itself a few times a second.
`run.sh` runs it with the OpenTelemetry Java agent, which sends its traces, logs and JVM metrics
(memory, garbage collection, threads, classes, CPU) straight to Leasyd.

    LEASYD_API_KEY=obs_... bash run.sh          # needs Java 17+

In Leasyd: **Service Map** > `payments-service` > **JVM dashboard**, or **Dashboards** > *Leasyd - JVM*.

Your own app: add `-javaagent:opentelemetry-javaagent.jar` and the `OTEL_*` settings from `run.sh`.
Behind the EC2 collector (`examples/ec2/setup.sh`), use `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`
and no API key header.
