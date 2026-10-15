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

Every APP_CHECK_EVERY_MIN minutes (15) it also checks what people sign in to: the web app (WEB_URL answers 200 with the app's page) and
the app API (a request without a token is refused with 401/403, so API Gateway and the sign-in
check are up), publishing obs/AppDown{part=web|api} = 1 when not.

Invoked with {"check": "routes"} (its own schedule, every 5 minutes, whether or not the data
canary runs): signs in as the canary tenant's own user (APP_USER, password in SSM
APP_PASSWORD_PARAM) and calls each backend behind the app API with a read that changes nothing
(ROUTES), publishing obs/AppRouteDown{route} and obs/AppRoutesDown (how many failed). A function
that can't even start (a broken deploy) fails only when called; this calls each one. Learned
2026-10-06: Synthetics answered 502 for 41 hours and no alarm fired, as nobody had called it.
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
WEB_URL = os.environ.get("WEB_URL", "")
APP_CHECK_EVERY_MIN = int(os.environ.get("APP_CHECK_EVERY_MIN", "15"))
SERVICE = "canary"
APP_CLIENT_ID = os.environ.get("APP_CLIENT_ID", "")
APP_USER = os.environ.get("APP_USER", "")
APP_PASSWORD_PARAM = os.environ.get("APP_PASSWORD_PARAM", "/obs/canary/app-password")
SCHEDULES_MAY_BE_OFF = set(filter(None, os.environ.get("SCHEDULES_MAY_BE_OFF", "").split(",")))
# (name, method, path, body): one read per backend function behind /v1/app.
ROUTES = [("account", "GET", "/v1/app/account", None),                  # obs-account-api
          ("checks", "GET", "/v1/app/checks", None),                    # obs-synthetics-api
          ("slos", "GET", "/v1/app/slos", None),
          ("windows", "GET", "/v1/app/windows", None),
          ("alert-rules", "GET", "/v1/app/alerts/rules", None),         # obs-alerts
          ("dashboards", "GET", "/v1/app/dashboards", None),
          ("query", "POST", "/v1/app/query", "query")]                  # obs-query-api (+ workers)

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


def _get(url):
    """(status, first 64 KB of the body) of a GET; status 0 when unreachable."""
    req = urllib.request.Request(url, headers={"User-Agent": "leasyd-canary"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except OSError:
        return 0, ""


def app_checks():
    """{part: 1 if down else 0} for the web app and the app API."""
    out = {}
    if WEB_URL:
        status, body = _get(WEB_URL)
        out["web"] = 0 if status == 200 and 'id="root"' in body else 1
    if ENDPOINT:
        out["api"] = 0 if _get(f"{ENDPOINT}/v1/app/account")[0] in (401, 403) else 1
    return out


def _request(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": token, "Content-Type": "application/json", "User-Agent": "leasyd-canary"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def route_checks(now=None):
    """{route: 1 if down else 0} for each of ROUTES, signed in as the canary's user. A failed
    sign-in counts every route down (the app can't be used either)."""
    now = now or time.time()
    try:
        password = boto3.client("ssm").get_parameter(Name=APP_PASSWORD_PARAM, WithDecryption=True)["Parameter"]["Value"]
        auth = boto3.client("cognito-idp").initiate_auth(
            ClientId=APP_CLIENT_ID, AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": APP_USER, "PASSWORD": password})
        token = auth["AuthenticationResult"]["IdToken"]
    except Exception as e:  # noqa: BLE001  any failure here is the app being unusable
        print(json.dumps({"routes_sign_in_failed": str(e)[:300]}))
        return {name: 1 for name, *_ in ROUTES}
    query = {"signal": "logs", "services": [SERVICE], "group_by": ["service"], "aggs": [{"fn": "count"}], "limit": 5,
             "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 900)),
             "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    out = {}
    for name, method, path, body in ROUTES:
        status = _request(method, f"{ENDPOINT}{path}", token, query if body == "query" else body)
        out[name] = 0 if status in (200, 202) else 1
        if out[name]:
            print(json.dumps({"route_down": name, "status": status}))
    return out


def schedules_off(events):
    """Names of obs-* schedules (EventBridge rules with a schedule) that are switched off, apart from
    SCHEDULES_MAY_BE_OFF (comma-separated names). None if they can't be read (counted as one off: alarm)."""
    try:
        rules, kw = [], {"NamePrefix": "obs-"}
        while True:
            page = events.list_rules(**kw)
            rules += page.get("Rules", [])
            if not page.get("NextToken"):
                break
            kw["NextToken"] = page["NextToken"]
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"schedules_unreadable": str(e)[:300]}))
        return None
    return sorted(r["Name"] for r in rules if r.get("ScheduleExpression") and r.get("State") != "ENABLED"
                  and r["Name"] not in SCHEDULES_MAY_BE_OFF)


def routes_handler(cw, events=None):
    down = route_checks()
    data = [{"MetricName": "AppRouteDown", "Dimensions": [{"Name": "route", "Value": r}], "Value": v} for r, v in down.items()]
    data.append({"MetricName": "AppRoutesDown", "Value": sum(down.values())})
    off = schedules_off(events or boto3.client("events"))
    data.append({"MetricName": "SchedulesOff", "Value": 1 if off is None else len(off)})
    if off:
        print(json.dumps({"schedules_off": off}))
    cw.put_metric_data(Namespace="obs", MetricData=data)
    print(json.dumps({"routes_down": [r for r, v in down.items() if v]}))
    return {"routes": down}


def _found(lam, signal, minute):
    t = minute * 60
    q = {"tenant": TENANT, "signal": signal, "services": [SERVICE], "match": {"trace_id": trace_id(signal, minute)},
         "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - 60)),
         "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t + 120))}
    out = json.loads(lam.invoke(FunctionName=LOOKUP, Payload=json.dumps(q).encode())["Payload"].read())
    return bool(out.get("files"))


def handler(event, context):
    if (event or {}).get("check") == "routes":
        return routes_handler(boto3.client("cloudwatch"))
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
    if minute % APP_CHECK_EVERY_MIN == 0:
        result["app_down"] = app_checks()
    data += [{"MetricName": "AppDown", "Dimensions": [{"Name": "part", "Value": part}], "Value": v}
             for part, v in result.get("app_down", {}).items()]
    cw.put_metric_data(Namespace="obs", MetricData=data)
    print(json.dumps({"minute": minute, **result}))
    return result
