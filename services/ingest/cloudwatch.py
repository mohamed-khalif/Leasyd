"""CloudWatch metrics from a customer's AWS account: a CloudWatch metric stream (JSON output
format) delivers through the customer's Firehose to POST /v1/aws/cloudwatch-metrics (Firehose's
HTTP endpoint destination; examples/aws/cloudwatch-metrics.yaml sets it up).

Firehose sends {"requestId", "timestamp", "records": [{"data": base64}]}; each record holds
newline-separated JSON lines, one per metric and minute:

  {"metric_stream_name": "...", "account_id": "123456789012", "region": "us-east-1",
   "namespace": "AWS/Lambda", "metric_name": "Duration", "dimensions": {"FunctionName": "checkout"},
   "timestamp": 1791072000000, "value": {"max": 812.0, "min": 3.1, "sum": 4210.5, "count": 37.0},
   "unit": "Milliseconds"}

Each line becomes an OpenTelemetry delta histogram with no buckets (count, sum, min, max over the
minute) named aws.<service>.<metric> (aws.lambda.duration, aws.sqs.number_of_messages_sent), so in
PromQL: sum_over_time(aws.lambda.invocations_sum[5m]), the average rate(x_sum) / rate(x_count). Dimensions become attributes (FunctionName, Resource, ...), plus
aws.namespace and aws.metric_name. A metric with a FunctionName dimension is under the service of
that name (as OpenTelemetry's Lambda layer names a function's traces); others under service
"aws-cloudwatch". The account and region are the resource's cloud.account.id and cloud.region.
"""
import base64
import json
import re

DEFAULT_SERVICE = "aws-cloudwatch"
MAX_LINES = 200_000

# CloudWatch unit -> UCUM, as OpenTelemetry writes units
UNITS = {"Seconds": "s", "Microseconds": "us", "Milliseconds": "ms", "Bytes": "By", "Kilobytes": "kBy",
         "Megabytes": "MBy", "Gigabytes": "GBy", "Terabytes": "TBy", "Bits": "bit", "Kilobits": "kbit",
         "Megabits": "Mbit", "Gigabits": "Gbit", "Terabits": "Tbit", "Percent": "%", "Count": "1",
         "Bytes/Second": "By/s", "Kilobytes/Second": "kBy/s", "Megabytes/Second": "MBy/s",
         "Gigabytes/Second": "GBy/s", "Terabytes/Second": "TBy/s", "Bits/Second": "bit/s",
         "Kilobits/Second": "kbit/s", "Megabits/Second": "Mbit/s", "Gigabits/Second": "Gbit/s",
         "Terabits/Second": "Tbit/s", "Count/Second": "1/s", "None": "1"}


class BadRequest(ValueError):
    pass


def snake(name):
    """ConcurrentExecutions -> concurrent_executions, CPUUtilization -> cpu_utilization."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def metric_name(namespace, name):
    """AWS/Lambda + Duration -> aws.lambda.duration; a custom namespace MyApp/Orders -> aws.myapp.orders.<metric>."""
    parts = [snake(p) for p in namespace.split("/") if p]
    if parts and parts[0] == "aws":
        parts = parts[1:]
    return ".".join(["aws", *parts, snake(name)])


def firehose_lines(body):
    """Firehose HTTP endpoint request body (bytes) -> (request id, JSON objects)."""
    try:
        req = json.loads(body)
    except ValueError as e:
        raise BadRequest(f"bad JSON body: {e}")
    if not isinstance(req, dict) or not isinstance(req.get("records"), list):
        raise BadRequest("expected a Firehose request: {requestId, timestamp, records: [{data}]}")
    out = []
    for rec in req["records"]:
        try:
            data = base64.b64decode((rec or {}).get("data") or "", validate=True)
        except (ValueError, TypeError) as e:
            raise BadRequest(f"bad record data: {e}")
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                raise BadRequest("records must be CloudWatch metric stream JSON lines (output format json)")
            if isinstance(obj, dict):
                out.append(obj)
            if len(out) > MAX_LINES:
                raise BadRequest("too many metrics in one request")
    return str(req.get("requestId") or ""), out


def _attr(k, v):
    return {"key": k, "value": {"stringValue": str(v)}}


def to_otlp(lines):
    """CloudWatch metric stream JSON lines -> an OTLP JSON metrics document."""
    resources = {}   # (account, region, service) -> {metric name -> metric}
    for m in lines:
        ns, name, value = m.get("namespace"), m.get("metric_name"), m.get("value")
        if not isinstance(ns, str) or not isinstance(name, str) or not isinstance(value, dict):
            continue
        try:
            t = int(m["timestamp"]) * 1_000_000
            count, total = float(value.get("count") or 0), float(value.get("sum") or 0)
            lo, hi = float(value.get("min") or 0), float(value.get("max") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        dims = m.get("dimensions") if isinstance(m.get("dimensions"), dict) else {}
        service = str(dims.get("FunctionName") or DEFAULT_SERVICE)
        key = (str(m.get("account_id") or ""), str(m.get("region") or ""), service, ns)
        metrics = resources.setdefault(key, {})
        otel_name = metric_name(ns, name)
        metric = metrics.setdefault(otel_name, {
            "name": otel_name, "unit": UNITS.get(m.get("unit"), "1"),
            "description": f"CloudWatch {ns} {name}",
            "histogram": {"aggregationTemporality": 1, "dataPoints": []}})
        metric["histogram"]["dataPoints"].append({
            "startTimeUnixNano": str(t - 60_000_000_000), "timeUnixNano": str(t),
            "attributes": [_attr("aws.namespace", ns), _attr("aws.metric_name", name)]
                          + [_attr(k, v) for k, v in sorted(dims.items())],
            "count": str(int(round(count))), "sum": total, "min": lo, "max": hi,
            "explicitBounds": [], "bucketCounts": [str(int(round(count)))]})
    out = []
    for (account, region, service, ns), metrics in sorted(resources.items()):
        attrs = [_attr("service.name", service), _attr("cloud.provider", "aws")]
        attrs += [_attr("cloud.account.id", account)] if account else []
        attrs += [_attr("cloud.region", region)] if region else []
        if ns == "AWS/Lambda" and service != DEFAULT_SERVICE:
            attrs += [_attr("cloud.platform", "aws_lambda"), _attr("faas.name", service)]
        out.append({"resource": {"attributes": attrs},
                    "scopeMetrics": [{"scope": {"name": "leasyd.cloudwatch"}, "metrics": list(metrics.values())}]})
    return {"resourceMetrics": out}
