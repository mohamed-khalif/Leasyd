# Querying your data: PromQL and SQL

Two ways to ask your own questions, both in the portal under **Query data**, and through the API
(`POST /v1/query` with a read key, or `/v1/app/query` signed in).

## PromQL (Query Builder)

PromQL across logs, spans and metrics. The Query Builder writes it for you (Tracing, Logging,
Metrics), or you write it yourself (PromQL tab).

| Series | What it is | Example |
|---|---|---|
| any metric by name | your OpenTelemetry metrics | `sum by (service_name) (rate(http_server_requests_total[5m]))` |
| `leasyd.spans` | one per span | `sum by (service_name) (rate(leasyd.spans{span_kind="SERVER"}[5m]))` |
| `leasyd.span.duration` | span durations (seconds) | `histogram_quantile(0.95, sum by (span_name) (rate(leasyd.span.duration[5m])))` |
| `<histogram>_bucket` | your histogram metrics' buckets | `histogram_quantile(0.95, sum by (le, service_name) (rate({"http.server.request.duration_bucket"}[5m])))` |
| `leasyd.logs` | one per log record | `sum by (service_name) (rate(leasyd.logs{severity_range="ERROR_FATAL"}[5m]))` |

- Metric names with dots: `{"http.server.requests"}`, or with underscores (`http_server_requests`).
  A counter's `_total` is optional; a histogram's `_count` and `_sum` are its count and sum.
- Labels are your attributes and resource attributes: `{"http.route"="/cart"}`, `by ("k8s.pod.name")`;
  underscores also match dots (`http_route`). Always there: `service_name`. Spans: `span_name`,
  `span_kind` (SERVER, CLIENT, INTERNAL, PRODUCER, CONSUMER), `status_code` (UNSET, OK, ERROR).
  Logs: `severity_text`, `severity_number`, `severity_range` (ERROR_FATAL, WARN, INFO, TRACE_DEBUG, UNKNOWN).
- In the Query Builder `$__interval` is the chart's step.
- Supported: `=`, `!=`, `=~`, `!~`; ranges and `offset`; `rate`, `increase`, `irate` (same as rate),
  `avg/min/max/sum/count/last_over_time`, `histogram_quantile` (span durations, and histogram metrics:
  linear inside the bucket like Prometheus, in the metric's own unit); `sum`, `avg`, `min`,
  `max`, `count`, `group`, `topk`, `bottomk`, `quantile`, `stddev`, `stdvar` with `by`/`without`;
  `+ - * / % ^`, comparisons (with `bool`), `and`, `or`, `unless`, `on()`, `ignoring()`;
  `abs`, `ceil`, `floor`, `round`, `sqrt`, `exp`, `ln`, `log2`, `log10`, `sgn`, `clamp`, `clamp_min`,
  `clamp_max`, `scalar`, `vector`, `time`.
- Not yet: exponential histograms in `histogram_quantile`, `group_left`/`group_right`,
  subqueries, `label_replace`/`label_join`, `absent`, `predict_linear`, `deriv`.
- Logs and spans have no series of their own: they are counted per `service_name` plus the labels
  you group by.
- Windows start and end on time-bucket edges (the step and ranges must be multiples of 10 seconds).
  Counter increases are exact: every rise is counted once, and restarts are handled.
- At most 10,000 series per query; aggregate with `sum by (...)` for more.

API: `{"promql": "...", "start": "2026-10-01T00:00:00Z", "end": "...", "step": 60}` (a range) or
`{"promql": "...", "time": ...}` (one moment). Times can be ISO-8601 or epoch seconds. The answer has
the Prometheus HTTP API's format.

## Check rules (alerts on any query)

From the Query Builder (**Create check rule**) or Alerts → New rule → *A query crosses a threshold*:
a PromQL query, a condition (above, below, ...), a critical threshold and optionally a degraded one.
Every series the query returns is checked on its own (e.g. one per service with `sum by (service_name)`):

- checked every 1, 5 or 15 minutes, on the data as of a minute before (it takes ~30 s to arrive);
- a series must stay degraded or critical for the chosen minutes (0-60) before you're told; a blip
  back to normal starts the wait again;
- you're told when series become degraded or critical, when they get worse, and when they're back to
  normal (or gone from the result); changes of the same kind come as one message, through your
  email, Slack or webhook channels, and are kept in Alerts → History;
- the query is run when the rule is saved, so a mistake shows at once. The rule form shows the last
  3 hours with the thresholds, and what each series would be right now.

Use a range in rates (`[5m]`); `$__interval` from the Query Builder becomes `5m`.

## SQL

One read-only `SELECT` in DuckDB SQL over three tables, for the chosen time range:

| Table | One row per | Main columns |
|---|---|---|
| `logs` | log record | `ts`, `service`, `severity_number`, `severity_text`, `body`, `trace_id`, `span_id`, `attributes`, `resource_attributes` |
| `spans` | span | `ts`, `end_ts`, `duration_ns`, `service`, `name`, `kind` (2 server, 3 client), `status_code` (2 error), `trace_id`, `span_id`, `parent_span_id`, `attributes`, `resource_attributes`, `events`, `links` |
| `metrics` | data point | `ts`, `service`, `metric_name`, `metric_type`, `unit`, `value`, `count`, `sum`, `min`, `max`, `bucket_counts`, `explicit_bounds`, `attributes`, `resource_attributes` |

Attributes: `attributes['http.route']`. Times are UTC. Joins, CTEs and window functions work. At most
1 GB read and 10,000 rows returned per query. Only a single `SELECT` over these tables is accepted:
no other tables, table functions (`read_csv`, ...), files, network or settings.

API: `{"sql": "SELECT ...", "start": "...", "end": "..."}` -> `{"columns", "rows", "truncated", "stats"}`.
