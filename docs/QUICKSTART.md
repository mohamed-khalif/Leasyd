# Send your data to Leasyd

Leasyd speaks **OpenTelemetry** (OTLP over HTTP). Any language with an OpenTelemetry SDK can send
traces, logs and metrics. There is no Leasyd library to install: you point the standard exporter at
Leasyd and add your API key.

Every example below was run against the live service on 2026-09-29, and its data was read back
through the query API (see [How this was tested](#how-this-was-tested)). A canary also sends a
log and a trace through `https://ingest.leasyd.com` every minute and alarms if they can't be found.

## What you need

| | |
|---|---|
| **Endpoint** | `https://ingest.leasyd.com` |
| **Account** | Sign up at https://app.leasyd.com ("Create an account"): free, 1 GB of data a day, kept 30 days. |
| **Ingest key** | In the app, **Settings → API keys → Create key** ("Send data"); shown once. It may only send data. A new key can take up to ~10 minutes to be fully active. |

## 1. Set four environment variables

The same four settings work in every language:

```sh
export OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.leasyd.com
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf          # http/json also works; grpc does not (see below)
export OTEL_EXPORTER_OTLP_HEADERS=x-api-key=<your ingest key>
export OTEL_SERVICE_NAME=checkout                         # how this service is named in Leasyd
```

The SDK adds `/v1/traces`, `/v1/logs` and `/v1/metrics` to the endpoint itself. Do not add them yourself.

## 2. Add OpenTelemetry to your app

Pick your language. Each example sends one trace, one log line linked to that trace, and one metric.

### Java: no code changes

Download the [OpenTelemetry Java agent](https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases)
and start your app with it:

```sh
java -javaagent:opentelemetry-javaagent.jar -jar your-app.jar
```

The agent automatically traces common libraries and frameworks (HTTP servers and clients, JDBC,
Kafka, Spring, and others). It also sends your `java.util.logging`, Logback and Log4j logs, and JVM
metrics.

### Python

```sh
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
```

```python
import logging
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

resource = Resource.create()  # service.name comes from OTEL_SERVICE_NAME

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)

meter_provider = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())])
metrics.set_meter_provider(meter_provider)

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
set_logger_provider(logger_provider)
logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))
logging.getLogger().setLevel(logging.INFO)

# --- your application ---
tracer = trace.get_tracer("checkout")
orders = metrics.get_meter("checkout").create_counter("orders_placed")
with tracer.start_as_current_span("place-order"):
    logging.info("order placed")          # linked to the trace automatically
    orders.add(1)

# Flush before exit (a long-running service doesn't need this).
tracer_provider.shutdown(); meter_provider.shutdown(); logger_provider.shutdown()
```

### Node.js

```sh
npm install @opentelemetry/sdk-node @opentelemetry/api @opentelemetry/api-logs
```

```js
// The SDK builds its exporters from the OTEL_* environment variables.
import { NodeSDK } from "@opentelemetry/sdk-node";
import { trace, metrics } from "@opentelemetry/api";
import { logs, SeverityNumber } from "@opentelemetry/api-logs";

const sdk = new NodeSDK();
sdk.start();

// --- your application ---
const tracer = trace.getTracer("checkout");
const orders = metrics.getMeter("checkout").createCounter("orders_placed");
const logger = logs.getLogger("checkout");
tracer.startActiveSpan("place-order", (span) => {
  logger.emit({ severityNumber: SeverityNumber.INFO, severityText: "INFO", body: "order placed" }); // linked to the trace
  orders.add(1);
  span.end();
});

// Flush before exit (a long-running service doesn't need this).
await sdk.shutdown();
```

### Go

```sh
go get go.opentelemetry.io/otel go.opentelemetry.io/otel/sdk go.opentelemetry.io/otel/sdk/metric go.opentelemetry.io/otel/sdk/log \
  go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp \
  go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp \
  go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp
```

```go
package main

import (
	"context"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/log"
	"go.opentelemetry.io/otel/log/global"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func main() {
	ctx := context.Background()
	res := resource.Default() // service.name comes from OTEL_SERVICE_NAME

	traceExp, err := otlptracehttp.New(ctx)
	if err != nil {
		panic(err)
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(traceExp), sdktrace.WithResource(res))
	otel.SetTracerProvider(tp)

	metricExp, err := otlpmetrichttp.New(ctx)
	if err != nil {
		panic(err)
	}
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(sdkmetric.NewPeriodicReader(metricExp)), sdkmetric.WithResource(res))
	otel.SetMeterProvider(mp)

	logExp, err := otlploghttp.New(ctx)
	if err != nil {
		panic(err)
	}
	lp := sdklog.NewLoggerProvider(sdklog.WithProcessor(sdklog.NewBatchProcessor(logExp)), sdklog.WithResource(res))
	global.SetLoggerProvider(lp)

	// --- your application ---
	orders, _ := otel.Meter("checkout").Int64Counter("orders_placed")
	logger := global.Logger("checkout")
	spanCtx, span := otel.Tracer("checkout").Start(ctx, "place-order")
	var rec log.Record
	rec.SetSeverity(log.SeverityInfo)
	rec.SetSeverityText("INFO")
	rec.SetBody(attribute.StringValue("order placed"))
	logger.Emit(spanCtx, rec) // linked to the trace
	orders.Add(spanCtx, 1)
	span.End()

	// Flush before exit (a long-running service doesn't need this).
	_ = tp.Shutdown(ctx)
	_ = mp.Shutdown(ctx)
	_ = lp.Shutdown(ctx)
}
```

### .NET, Ruby, PHP, Rust, Erlang/Elixir, Swift, C++

Use that language's OpenTelemetry SDK with its OTLP **HTTP** exporter and the environment variables
from step 1. We have not yet run these languages against Leasyd ourselves.

## Already using the OpenTelemetry Collector, gRPC, or another agent?

Leasyd does not accept OTLP over **gRPC** (port 4317). If your apps can't switch to `http/protobuf`,
or you already run an [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/), let the
Collector forward to Leasyd:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch: {}

exporters:
  otlphttp/leasyd:
    endpoint: https://ingest.leasyd.com
    headers:
      x-api-key: ${env:LEASYD_API_KEY}

service:
  pipelines:
    traces:  { receivers: [otlp], processors: [batch], exporters: [otlphttp/leasyd] }
    logs:    { receivers: [otlp], processors: [batch], exporters: [otlphttp/leasyd] }
    metrics: { receivers: [otlp], processors: [batch], exporters: [otlphttp/leasyd] }
```

Run it with `LEASYD_API_KEY=<your ingest key>`. Point your apps at the Collector (gRPC `:4317` or
HTTP `:4318`). Collector v0.162 and newer log a warning that `otlphttp` is being renamed to
`otlp_http`; either name works there, but only `otlphttp` works on older versions.

The Collector (the `otelcol-contrib` build) can also receive data from other agents, such as
Prometheus, Fluent Bit / Fluentd, Jaeger, Zipkin and syslog, and convert it to OpenTelemetry.
Leasyd does not accept those agents' own formats directly, and we have not yet tested those routes.

## Check it worked

Open the Leasyd app and choose the last 15 minutes. Your service shows up within about a minute.
From the **Logs** page, open a log line and click its `trace_id` to see the whole trace.

If nothing shows up, check the HTTP status your exporter logs:

| Status | Meaning |
|---|---|
| `401` `Unauthorized` | The key is missing or wrong. Check `OTEL_EXPORTER_OTLP_HEADERS`. |
| `403` `Missing Authentication Token` | The URL is wrong. Set the endpoint to exactly `https://ingest.leasyd.com`; the SDK adds `/v1/...` itself. |
| `403` `not authorized` | It is a read-only key, which can query but not send. Use your ingest key. |
| `403` `Forbidden` | A brand-new key can be refused on some requests for up to ~10 minutes. The SDK retries; the data gets through. |
| `400` / `415` | The body isn't OTLP. Use protocol `http/protobuf` or `http/json`. |
| `429` | Too many requests. The standard plan allows 500 requests/s per key, with bursts up to 1,000. SDKs retry these automatically. |

Requests may be gzip-compressed, which the SDKs do by default in most languages. Requests over about
4.5 MB are refused; the SDKs' default batch sizes are far smaller.

## How this was tested

On 2026-09-29 each example above was run with a fresh ingest key for a test account. Its data was
then read back with that account's read key through `POST /v1/query`, which checked:

- **Traces, logs and metrics arrived** for each service, within about a minute:
  `qs-python`, `qs-node` (`@opentelemetry/sdk-node` 0.222), `qs-go` (otel-go 1.46 / log 0.22),
  `qs-java` (Java agent 2.31.1: a traced HTTP client call, a log line and 42 JVM metric points), and
  `qs-collector-grpc` (a Python app sending over gRPC through Collector v0.162).
- **Logs link to traces:** searching logs by the Python trace ID returned that trace's log line.
- **Error codes** in the table above: each case (wrong path, missing stage, wrong content type,
  corrupt body, read key, missing key, bad key) was sent and its response recorded.
