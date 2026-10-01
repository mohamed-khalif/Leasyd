// Ready-made dashboards over standard OpenTelemetry data and Leasyd's own series. Read-only:
// "Clone" makes an editable copy for the tenant. $service_name is the dashboard's service filter.
import type { Dashboard } from "./api";

const S = 'service_name=~"$service_name"';
const svc = [{ name: "service_name", label: "Service Name" }];
const text = (id: string, title: string, body: string, w = 3, h = 2) => ({ id, type: "text" as const, title, text: body, w, h });

export const BUILTIN: Dashboard[] = [
  {
    id: "builtin-services", builtin: true, version: 1, name: "Leasyd - Service overview",
    description: "Requests, errors and duration (RED) for every service, from its spans; and its logs.",
    variables: svc,
    panels: [
      text("about", "Overview", "Rate, errors and duration of each service, worked out from its spans (leasyd.spans, leasyd.span.duration), and its logs by severity.\n\nFilter with Service Name above.", 3, 2),
      { id: "rate", type: "timeseries", title: "Requests", description: "Server spans per second: requests each service received", w: 5, h: 2, unit: "/s",
        queries: [{ promql: `sum by (service_name) (rate(leasyd.spans{${S}, span_kind="SERVER"}[$__interval]))`, legend: "{{service_name}}" }] },
      { id: "errors", type: "timeseries", title: "Errors", description: "Share of spans that failed (0 when none did)", w: 4, h: 2, unit: "%",
        queries: [{ promql: `100 * (sum by (service_name) (rate(leasyd.spans{${S}, status_code="ERROR"}[$__interval])) or 0 * sum by (service_name) (rate(leasyd.spans{${S}}[$__interval]))) / sum by (service_name) (rate(leasyd.spans{${S}}[$__interval]))`, legend: "{{service_name}}" }] },
      text("aboutduration", "Duration", "How long requests take: the median (p50), and the slowest 5% and 1% (p95, p99).", 3, 2),
      { id: "p95", type: "timeseries", title: "Duration p95", description: "95% of server spans are faster than this", w: 5, h: 2, unit: "s",
        queries: [{ promql: `histogram_quantile(0.95, sum by (service_name) (rate(leasyd.span.duration{${S}, span_kind="SERVER"}[$__interval])))`, legend: "{{service_name}}" }] },
      { id: "pall", type: "timeseries", title: "Duration p50 / p95 / p99", description: "All services in the filter together", w: 4, h: 2, unit: "s",
        queries: [{ promql: `histogram_quantile(0.5, sum(rate(leasyd.span.duration{${S}, span_kind="SERVER"}[$__interval])))`, legend: "p50" },
                  { promql: `histogram_quantile(0.95, sum(rate(leasyd.span.duration{${S}, span_kind="SERVER"}[$__interval])))`, legend: "p95" },
                  { promql: `histogram_quantile(0.99, sum(rate(leasyd.span.duration{${S}, span_kind="SERVER"}[$__interval])))`, legend: "p99" }] },
      { id: "slowest", type: "timeseries", title: "Slowest operations", description: "The 5 operations with the highest p95", w: 6, h: 2, unit: "s",
        queries: [{ promql: `topk(5, histogram_quantile(0.95, sum by (span_name) (rate(leasyd.span.duration{${S}, span_kind="SERVER"}[$__interval]))))`, legend: "{{span_name}}" }] },
      { id: "logs", type: "bars", title: "Logs by severity", description: "Log records per interval", w: 6, h: 2,
        queries: [{ promql: `sum by (severity_range) (increase(leasyd.logs{${S}}[$__interval]))`, legend: "{{severity_range}}" }] },
      { id: "spansnow", type: "stat", title: "Requests now", description: "Server spans per second, all services in the filter", w: 3, h: 1, unit: "/s",
        queries: [{ promql: `sum(rate(leasyd.spans{${S}, span_kind="SERVER"}[5m]))` }] },
      { id: "errorsnow", type: "stat", title: "Errors now", description: "Share of spans that failed, last 5 minutes", w: 3, h: 1, unit: "%",
        queries: [{ promql: `100 * (sum(rate(leasyd.spans{${S}, status_code="ERROR"}[5m])) or 0 * sum(rate(leasyd.spans{${S}}[5m]))) / sum(rate(leasyd.spans{${S}}[5m]))` }] },
      { id: "p95now", type: "stat", title: "p95 now", description: "Server spans, last 5 minutes", w: 3, h: 1, unit: "s",
        queries: [{ promql: `histogram_quantile(0.95, sum(rate(leasyd.span.duration{${S}, span_kind="SERVER"}[5m])))` }] },
      { id: "errorlogsnow", type: "stat", title: "Error logs now", description: "ERROR and FATAL records per second, last 5 minutes", w: 3, h: 1, unit: "/s",
        queries: [{ promql: `sum(rate(leasyd.logs{${S}, severity_range="ERROR_FATAL"}[5m]))` }] },
    ],
  },
  {
    id: "builtin-runtime", builtin: true, version: 1, name: "Leasyd - Process & HTTP metrics",
    description: "OpenTelemetry semantic-convention metrics: process CPU and memory, HTTP server requests and their duration, database connections.",
    variables: svc,
    panels: [
      text("about", "Process", "process.cpu.utilization and process.memory.usage, as OpenTelemetry SDKs and the Collector report them.", 3, 2),
      { id: "cpu", type: "timeseries", title: "CPU", description: "process.cpu.utilization (share of one CPU)", w: 5, h: 2, unit: "percentunit",
        queries: [{ promql: `avg by (service_name) (avg_over_time({"process.cpu.utilization", ${S}}[$__interval]))`, legend: "{{service_name}}" }] },
      { id: "mem", type: "timeseries", title: "Memory", description: "process.memory.usage", w: 4, h: 2, unit: "bytes",
        queries: [{ promql: `sum by (service_name) (avg_over_time({"process.memory.usage", ${S}}[$__interval]))`, legend: "{{service_name}}" }] },
      text("abouthttp", "HTTP server", "http.server.request.duration: how many requests each service handled, and how long they took on average.", 3, 2),
      { id: "httprate", type: "timeseries", title: "HTTP requests", description: "Requests per second (the histogram's count)", w: 5, h: 2, unit: "/s",
        queries: [{ promql: `sum by (service_name) (rate({"http.server.request.duration_count", ${S}}[$__interval]))`, legend: "{{service_name}}" }] },
      { id: "httpavg", type: "timeseries", title: "HTTP duration (average)", description: "The histogram's sum over its count, in the metric's own unit (OpenTelemetry's standard is seconds; some SDKs report ms)", w: 4, h: 2, unit: "ms",
        queries: [{ promql: `sum by (service_name) (rate({"http.server.request.duration_sum", ${S}}[$__interval])) / sum by (service_name) (rate({"http.server.request.duration_count", ${S}}[$__interval]))`, legend: "{{service_name}}" }] },
      { id: "httproute", type: "bars", title: "HTTP requests by route", description: "Requests per interval, by http.route", w: 6, h: 2,
        queries: [{ promql: `sum by (http_route) (increase({"http.server.request.duration_count", ${S}}[$__interval]))`, legend: "{{http_route}}" }] },
      { id: "db", type: "timeseries", title: "Database connections in use", description: "db.client.connections.usage", w: 6, h: 2,
        queries: [{ promql: `sum by (service_name) (avg_over_time({"db.client.connections.usage", ${S}}[$__interval]))`, legend: "{{service_name}}" }] },
    ],
  },
  {
    id: "builtin-synthetics", builtin: true, version: 1, name: "Leasyd - Synthetic checks",
    description: "Uptime and response time of your synthetic checks (excluded runs left out).",
    variables: [],
    panels: [
      { id: "uptime", type: "timeseries", title: "Uptime", description: "Share of runs that passed", w: 6, h: 2, unit: "%",
        queries: [{ promql: '100 * avg by (check_name) (avg_over_time({"synthetics.check.success", "check.excluded"=""}[$__interval]))', legend: "{{check_name}}" }] },
      { id: "duration", type: "timeseries", title: "Response time", description: "Time for all of a run's steps", w: 6, h: 2, unit: "ms",
        queries: [{ promql: 'avg by (check_name) (avg_over_time({"synthetics.check.duration"}[$__interval]))', legend: "{{check_name}}" }] },
      { id: "fails", type: "bars", title: "Failed runs", description: "Runs that failed, per interval", w: 6, h: 2,
        queries: [{ promql: 'sum by (check_name) (count_over_time({"synthetics.check.success", "check.excluded"=""}[$__interval]) - sum_over_time({"synthetics.check.success", "check.excluded"=""}[$__interval]))', legend: "{{check_name}}" }] },
      { id: "tls", type: "stat", title: "TLS certificate (days left)", description: "Days until the soonest certificate expires", w: 3, h: 2,
        queries: [{ promql: 'min(last_over_time({"synthetics.check.tls_days_remaining"}[1h]))' }] },
      { id: "uptimenow", type: "stat", title: "Uptime (24 h)", description: "Every run of every check together, including checks deleted since", w: 3, h: 2, unit: "%",
        queries: [{ promql: '100 * avg(avg_over_time({"synthetics.check.success", "check.excluded"=""}[24h]))' }] },
    ],
  },
];

export const isBuiltin = (id: string) => id.startsWith("builtin-");
