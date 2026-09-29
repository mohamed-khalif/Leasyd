"""Freshness canary: runs every minute, end to end through the real endpoint.

Each run, for logs and traces:
  1. sends one record for the "canary" tenant whose trace id is derived from
     the current minute (so later runs know what to look for), and
  2. looks up the record sent CHECK_AFTER_MIN minutes earlier and publishes
     obs/CanaryMissing{signal} = 1 if it is not findable, else 0.

An alarm on three consecutive misses means data is not becoming searchable
within ~2 minutes, whatever the cause (ingest, Firehose, fast lane, index,
lookup). The API key is read from SSM (SecureString KEY_PARAM).

Each run also sends metrics about itself (so the metrics path is exercised,
and the canary tenant's metrics explorer shows real data): canary.runs (a
cumulative counter), canary.ingest.duration (a gauge: how long each send
took, per signal) and canary.ingest.latency (a delta histogram of the same).
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

import boto3

ENDPOINT = os.environ.get("INGEST_ENDPOINT", "")
KEY_PARAM = os.environ.get("KEY_PARAM", "/obs/canary/api-key")
TENANT = os.environ.get("CANARY_TENANT", "canary")
LOOKUP = os.environ.get("LOOKUP_FUNCTION", "obs-index-lookup")
CHECK_AFTER_MIN = int(os.environ.get("CHECK_AFTER_MIN", "2"))
SERVICE = "canary"

_key = None


def trace_id(signal, minute):
    """The canary's trace id for a signal and a minute (epoch seconds // 60)."""
    return hashlib.sha256(f"obs-canary/{signal}/{minute}".encode()).hexdigest()[:32]


def record(signal, minute, now_ns):
    """One OTLP JSON request holding a single log record or span."""
    res = {"attributes": [{"key": "service.name", "value": {"stringValue": SERVICE}}]}
    tid = trace_id(signal, minute)
    if signal == "logs":
        return {"resourceLogs": [{"resource": res, "scopeLogs": [{"logRecords": [{
            "timeUnixNano": str(now_ns), "severityNumber": 9, "body": {"stringValue": f"canary {minute}"},
            "traceId": tid}]}]}]}
    return {"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": [{
        "traceId": tid, "spanId": tid[:16], "name": "canary", "kind": 1,
        "startTimeUnixNano": str(now_ns), "endTimeUnixNano": str(now_ns + 1_000_000)}]}]}]}


RUNS_SINCE_MINUTE = 29_280_000   # 2025-09-02: canary.runs counts minutes since then (one run a minute)


def metrics(minute, now_ns, durations_ms):
    """One OTLP JSON request: the canary's own metrics. durations_ms: {signal: send time in ms}."""
    res = {"attributes": [{"key": "service.name", "value": {"stringValue": SERVICE}}]}
    def point(**kw):
        return {"timeUnixNano": str(now_ns), **kw}
    def by_signal(s):
        return [{"key": "signal", "value": {"stringValue": s}}]
    ms = list(durations_ms.values())
    return {"resourceMetrics": [{"resource": res, "scopeMetrics": [{"scope": {"name": "canary"}, "metrics": [
        {"name": "canary.runs", "description": "Canary runs", "unit": "{run}", "sum": {
            "aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [point(
                startTimeUnixNano=str(RUNS_SINCE_MINUTE * 60 * 10**9), asInt=str(minute - RUNS_SINCE_MINUTE))]}},
        {"name": "canary.ingest.duration", "description": "Time to send one record to the ingest endpoint",
         "unit": "ms", "gauge": {"dataPoints": [point(asDouble=d, attributes=by_signal(s)) for s, d in durations_ms.items()]}},
        {"name": "canary.ingest.latency", "description": "Time to send one record to the ingest endpoint",
         "unit": "ms", "histogram": {"aggregationTemporality": 1, "dataPoints": [point(
             startTimeUnixNano=str(now_ns - 60 * 10**9), count=str(len(ms)), sum=sum(ms),
             min=min(ms, default=0), max=max(ms, default=0),
             bucketCounts=[str(sum(1 for d in ms if lo < d <= hi)) for lo, hi in zip([-1, 50, 100, 250, 500], [50, 100, 250, 500, 1e12])],
             explicitBounds=[50, 100, 250, 500])]}},
    ]}]}]}


def _api_key():
    global _key
    if _key is None:
        _key = boto3.client("ssm").get_parameter(Name=KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _key


def _send(signal, body):
    req = urllib.request.Request(f"{ENDPOINT}/v1/{signal}", data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": _api_key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def _found(lam, signal, minute):
    t = minute * 60
    q = {"tenant": TENANT, "signal": signal, "services": [SERVICE], "match": {"trace_id": trace_id(signal, minute)},
         "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - 60)),
         "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t + 120))}
    out = json.loads(lam.invoke(FunctionName=LOOKUP, Payload=json.dumps(q).encode())["Payload"].read())
    return bool(out.get("files"))


def handler(event, context):
    now = time.time()
    minute = int(now // 60)
    lam, cw = boto3.client("lambda"), boto3.client("cloudwatch")
    result, data, durations = {}, [], {}
    for signal in ("logs", "traces"):
        t0 = time.perf_counter()
        status = _send(signal, record(signal, minute, time.time_ns()))
        durations[signal] = round((time.perf_counter() - t0) * 1000, 1)
        missing = 0 if _found(lam, signal, minute - CHECK_AFTER_MIN) else 1
        result[signal] = {"sent": status, "missing": missing}
        dims = [{"Name": "signal", "Value": signal}]
        data += [{"MetricName": "CanaryMissing", "Dimensions": dims, "Value": missing},
                 {"MetricName": "CanarySendFailed", "Dimensions": dims, "Value": 0 if status == 200 else 1}]
    status = _send("metrics", metrics(minute, time.time_ns(), durations))
    result["metrics"] = {"sent": status}
    data.append({"MetricName": "CanarySendFailed", "Dimensions": [{"Name": "signal", "Value": "metrics"}],
                 "Value": 0 if status == 200 else 1})
    cw.put_metric_data(Namespace="obs", MetricData=data)
    print(json.dumps({"minute": minute, **result}))
    return result
