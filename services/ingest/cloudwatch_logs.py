"""CloudWatch Logs from a customer's AWS account: subscription filters on their log groups deliver
through the customer's Firehose to POST /v1/aws/cloudwatch-logs (Firehose's HTTP endpoint destination;
examples/aws/cloudwatch-logs.yaml sets it up). Design: docs/cloudwatch-logs-design.md.

Firehose sends {"requestId", "timestamp", "records": [{"data": base64}]}; each record's data is a
CloudWatch Logs subscription message, gzipped (or plain, when the customer turned on Firehose's
decompression):

  {"messageType": "DATA_MESSAGE", "owner": "123456789012", "logGroup": "/aws/lambda/checkout",
   "logStream": "2026/10/18/[$LATEST]ab12", "subscriptionFilters": ["leasyd"],
   "logEvents": [{"id": "3796...", "timestamp": 1791072000123, "message": "..."}]}

CONTROL_MESSAGE records (AWS checking the destination) are skipped. The region is not in the
message: the template sends it as the Firehose common attribute cloud.region.

Each event becomes an OTLP log record. Its service comes from the log group: /aws/lambda/<fn> ->
<fn> (as OpenTelemetry's Lambda layer names a function's traces); an awslogs stream
<prefix>/<container>/<task> -> the container; /aws/rds/instance|cluster/<db>/<type> -> <db>;
API-Gateway-Execution-Logs_<id>/<stage> -> apigateway-<id>; anything else the log group name.
A JSON message (structured logging) gives the body (message / msg) and attributes; severity comes
from a level field or a leading keyword; a trace id (trace_id, traceId, or an X-Ray id) links the
log to its trace. Lambda REPORT lines carry duration, memory and cold start as attributes.
"""
import base64
import gzip
import io
import json
import re

MAX_EVENTS = 100_000
MAX_RECORD_BYTES = 16 * 1024 * 1024     # one decompressed subscription message
MAX_JSON_ATTRS = 50
MAX_ATTR_CHARS = 2000

SEVERITY = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "NOTICE": 10, "WARN": 13, "WARNING": 13,
            "ERROR": 17, "ERR": 17, "CRITICAL": 21, "FATAL": 21, "PANIC": 21}
SEVERITY_TEXT = {1: "TRACE", 5: "DEBUG", 9: "INFO", 10: "INFO", 13: "WARN", 17: "ERROR", 21: "FATAL"}

# [ERROR] ... (Python on Lambda), <time>\t<request id>\tERROR\t... (Node), or ERROR / WARN: / level=error at the start
_LEVEL_WORD = r"(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRITICAL|FATAL|PANIC)"
_LEVEL_BRACKET = re.compile(r"^\s*\[" + _LEVEL_WORD + r"\]", re.I)
_LEVEL_TAB = re.compile(r"^\S+\t[0-9a-f-]{36}\t" + _LEVEL_WORD + r"\t", re.I)
_LEVEL_LEAD = re.compile(r"^\s*(?:\S+\s+){0,2}?" + _LEVEL_WORD + r"\b[:\s]")    # upper case only: "Info about" isn't a level
_LEVEL_KV = re.compile(r"\blevel=\"?" + _LEVEL_WORD + r"\b", re.I)
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_XRAY = re.compile(r"\b1-([0-9a-f]{8})-([0-9a-f]{24})\b")
_REQUEST_ID = r"RequestId: ([0-9a-f-]{36})"
_REPORT_FIELDS = {"Duration": "aws.lambda.duration_ms", "Billed Duration": "aws.lambda.billed_duration_ms",
                  "Memory Size": "aws.lambda.memory_size_mb", "Max Memory Used": "aws.lambda.max_memory_used_mb",
                  "Init Duration": "aws.lambda.init_duration_ms"}
_PLATFORM_METRICS = {"durationMs": "aws.lambda.duration_ms", "billedDurationMs": "aws.lambda.billed_duration_ms",
                     "memorySizeMB": "aws.lambda.memory_size_mb", "maxMemoryUsedMB": "aws.lambda.max_memory_used_mb",
                     "initDurationMs": "aws.lambda.init_duration_ms"}
_ECS_STREAM = re.compile(r"^([^/]+)/([^/]+)/([0-9a-f]{32}|[0-9a-f-]{36})$")
_RDS = re.compile(r"^/aws/rds/(?:instance|cluster)/([^/]+)/([^/]+)$")
_APIGW = re.compile(r"^API-Gateway-Execution-Logs_([^/]+)/(.+)$")


class BadRequest(ValueError):
    pass


def firehose_messages(body):
    """Firehose HTTP endpoint request body (bytes) -> (request id, subscription DATA_MESSAGEs)."""
    try:
        req = json.loads(body)
    except ValueError as e:
        raise BadRequest(f"bad JSON body: {e}")
    if not isinstance(req, dict) or not isinstance(req.get("records"), list):
        raise BadRequest("expected a Firehose request: {requestId, timestamp, records: [{data}]}")
    out, events = [], 0
    for rec in req["records"]:
        try:
            data = base64.b64decode((rec or {}).get("data") or "", validate=True)
        except (ValueError, TypeError) as e:
            raise BadRequest(f"bad record data: {e}")
        for msg in _messages(data):
            if msg.get("messageType") != "DATA_MESSAGE" or not isinstance(msg.get("logEvents"), list):
                continue
            events += len(msg["logEvents"])
            if events > MAX_EVENTS:
                raise BadRequest("too many log events in one request")
            out.append(msg)
    return str(req.get("requestId") or ""), out


def _messages(data):
    """One record's data -> its subscription messages: gzipped or plain JSON; with Firehose's decompression,
    several messages may share a record (one per line, or back to back)."""
    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
                data = f.read(MAX_RECORD_BYTES + 1)
        except (OSError, EOFError) as e:
            raise BadRequest(f"bad gzip record: {e}")
    if len(data) > MAX_RECORD_BYTES:
        raise BadRequest("a record is too large once decompressed")
    text, dec, pos, out = data.decode("utf-8", "replace"), json.JSONDecoder(), 0, []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return out
        try:
            obj, pos = dec.raw_decode(text, pos)
        except ValueError:
            raise BadRequest("records must be CloudWatch Logs subscription messages (from a subscription filter)")
        if isinstance(obj, dict):
            out.append(obj)


def service_of(group, stream):
    """(service name, extra attributes) from a log group and stream."""
    if group.startswith("/aws/lambda/") and len(group) > len("/aws/lambda/"):
        fn = group[len("/aws/lambda/"):]
        return fn, {"cloud.platform": "aws_lambda", "faas.name": fn}
    m = _ECS_STREAM.match(stream or "")
    if m:
        return m.group(2), {"cloud.platform": "aws_ecs", "container.name": m.group(2), "aws.ecs.task.id": m.group(3)}
    m = _RDS.match(group)
    if m:
        return m.group(1), {"cloud.platform": "aws_rds", "db.log.type": m.group(2)}
    m = _APIGW.match(group)
    if m:
        return f"apigateway-{m.group(1)}", {"cloud.platform": "aws_api_gateway", "aws.apigateway.stage": m.group(2)}
    return group.lstrip("/") or "aws-cloudwatch-logs", {}


def _value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if not isinstance(v, str):
        v = json.dumps(v, separators=(",", ":"), default=str)
    return {"stringValue": v[:MAX_ATTR_CHARS]}


def _attrs(d):
    return [{"key": k, "value": _value(v)} for k, v in d.items() if v not in ("", None)]


def _num(s):
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _severity(text):
    t = str(text or "").strip().upper()
    if t in SEVERITY:
        return SEVERITY[t]
    n = _num(t)
    if isinstance(n, int) and 1 <= n <= 24:     # already an OpenTelemetry severity number
        return n
    return 0


def _text_severity(msg):
    for rx in (_LEVEL_BRACKET, _LEVEL_TAB, _LEVEL_LEAD, _LEVEL_KV):
        m = rx.search(msg[:300])
        if m:
            return SEVERITY[m.group(1).upper()]
    return 0


def _trace(v):
    """A trace id as 32 hex characters: an OpenTelemetry one, or an X-Ray one (1-<8 hex>-<24 hex>)."""
    s = str(v or "").strip().lower()
    if _HEX32.match(s):
        return s
    m = _XRAY.search(s)
    return m.group(1) + m.group(2) if m else ""


def _lambda_report(msg, attrs):
    """START / END / REPORT lines (text format) -> attributes; REPORT's numbers as numbers."""
    m = re.match(r"^(START|END|REPORT) " + _REQUEST_ID, msg)
    if not m:
        return
    attrs["faas.invocation_id"] = m.group(2)
    attrs["aws.lambda.platform"] = m.group(1).lower()
    if m.group(1) == "REPORT":
        for label, key in _REPORT_FIELDS.items():
            f = re.search(r"\t" + label + r": ([0-9.]+)", msg)
            if f and _num(f.group(1)) is not None:
                attrs[key] = _num(f.group(1))
        attrs["faas.coldstart"] = "aws.lambda.init_duration_ms" in attrs


def _structured(obj, attrs):
    """A JSON message -> (body, severity, trace id, span id); its other top-level fields become attributes."""
    body = obj.get("message", obj.get("msg"))
    sev = _severity(obj.get("level") or obj.get("severity") or obj.get("levelname") or obj.get("log.level"))
    trace = _trace(obj.get("trace_id") or obj.get("traceId") or obj.get("traceid") or obj.get("xray_trace_id")
                   or obj.get("AWS-XRAY-TRACE-ID") or "")
    span = str(obj.get("span_id") or obj.get("spanId") or "").lower()
    if obj.get("type") == "platform.report" and isinstance(obj.get("record"), dict):     # Lambda JSON log format
        rec = obj["record"]
        metrics = rec.get("metrics") if isinstance(rec.get("metrics"), dict) else {}
        attrs.update({"faas.invocation_id": rec.get("requestId"), "aws.lambda.platform": "report"})
        attrs.update({key: _num(metrics[k]) for k, key in _PLATFORM_METRICS.items() if _num(metrics.get(k)) is not None})
        attrs["faas.coldstart"] = "aws.lambda.init_duration_ms" in attrs
        body = body or f"REPORT RequestId: {rec.get('requestId')}"
    elif str(obj.get("type") or "").startswith("platform.") and isinstance(obj.get("record"), dict):
        attrs.update({"faas.invocation_id": obj["record"].get("requestId"),
                      "aws.lambda.platform": obj["type"][len("platform."):]})
        body = body or obj["type"]
    skip = {"message", "msg", "level", "severity", "levelname", "log.level", "trace_id", "traceId", "traceid",
            "span_id", "spanId", "timestamp", "time", "record", "type"}
    for k, v in list(obj.items())[:MAX_JSON_ATTRS]:
        if k not in skip and v not in ("", None):
            attrs.setdefault("faas.invocation_id" if k == "requestId" else str(k)[:100], v)
    if body is None:
        body = json.dumps(obj, separators=(",", ":"))
    return body if isinstance(body, str) else json.dumps(body, default=str), sev, trace, span if _HEX16.match(span) else ""


def to_record(ev):
    """One subscription logEvent -> an OTLP JSON log record (None if it has no usable timestamp)."""
    try:
        t = int(ev["timestamp"]) * 1_000_000
    except (KeyError, TypeError, ValueError):
        return None
    msg = str(ev.get("message") or "").rstrip("\r\n")
    attrs, sev, trace, span, body, structured = {"aws.log.event.id": ev.get("id")}, 0, "", "", msg, False
    s = msg.lstrip()
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            body, sev, trace, span = _structured(obj, attrs)
            structured = True
    if not structured:
        _lambda_report(msg, attrs)
        sev = 9 if "aws.lambda.platform" in attrs else _text_severity(msg)
        trace = _trace(msg) if _XRAY.search(msg) else ""
    rec = {"timeUnixNano": str(t), "body": {"stringValue": body}, "attributes": _attrs(attrs)}
    if sev:
        rec["severityNumber"] = sev
        rec["severityText"] = SEVERITY_TEXT.get(sev, "")
    if trace:
        rec["traceId"] = trace
        if span:
            rec["spanId"] = span
    return rec


def to_otlp(messages, region=""):
    """Subscription DATA_MESSAGEs -> an OTLP JSON logs document, one resource per (account, group, stream's service)."""
    resources = {}
    for m in messages:
        group, stream = str(m.get("logGroup") or ""), str(m.get("logStream") or "")
        service, extra = service_of(group, stream)
        key = (str(m.get("owner") or ""), group, service, tuple(sorted(extra.items())))
        res = resources.get(key)
        if res is None:
            attrs = {"service.name": service, "cloud.provider": "aws", "cloud.account.id": key[0],
                     "cloud.region": region, "aws.log.group.names": group, **extra}
            res = resources[key] = {"resource": {"attributes": _attrs(attrs)},
                                    "scopeLogs": [{"scope": {"name": "leasyd.cloudwatch-logs"}, "logRecords": []}]}
        for ev in m["logEvents"]:
            rec = to_record(ev) if isinstance(ev, dict) else None
            if rec:
                rec["attributes"].append({"key": "aws.log.stream.names", "value": {"stringValue": stream}})
                res["scopeLogs"][0]["logRecords"].append(rec)
    return {"resourceLogs": [r for r in resources.values() if r["scopeLogs"][0]["logRecords"]]}
