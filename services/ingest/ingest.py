"""OTLP/HTTP ingest Lambda (behind API Gateway and the API-key authorizer).

POST /v1/logs | /v1/traces | /v1/metrics, body OTLP protobuf or JSON,
optionally gzip-encoded. POST /v1/aws/cloudwatch-metrics takes a customer's
CloudWatch metric stream through their Firehose (cloudwatch.py) as metrics. The request is converted to one OTLP JSON line per
Firehose record and put on the tenant's own delivery stream
(obs-t-<tenant>-<signal>), which batches it into
s3://.../_incoming/tenant=<T>/<signal>/dt=/hour=/ for compaction.

- Tenant: only from the authorizer's context (i.e. from the API key). Nothing
  the client sends can choose it; obs.* resource attributes from the client
  are stripped.
- Daily caps: bytes (OTLP JSON, before compression) are metered per tenant and UTC day in
  obs-tenants (meter#<tenant>#<day>, flushed every METER_FLUSH_S per container). A tenant
  whose record has daily_cap_bytes (the free plan's 1 GB) gets 429 with Retry-After once
  today's bytes reach it, until midnight UTC. Metering never fails a request: if DynamoDB is
  unreachable the data is accepted (and counted when it can be).
- Durability: Firehose has stored the records durably when PutRecordBatch
  succeeds, so the client gets 200 only then. On failure it gets 503 and its
  SDK retries. (At least once: a retry after a partial failure can duplicate
  the records that did succeed.)
"""

import base64
import gzip
import io
import json
import os
import re
import time

import boto3
from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

import cloudwatch
import droprules

STREAM_PREFIX = os.environ.get("STREAM_PREFIX", "obs-t-")
TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "")   # empty: no metering, no caps
METER_FLUSH_S = 10    # how often a container adds its counts to the day's meter
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "mkhalif@leasyd.com")   # where an ended trial is told to write
CAP_CHECK_S = 30      # how long a container trusts what it read of a tenant's cap and usage
RECORD_COMPRESSION = os.environ.get("RECORD_COMPRESSION", "none")   # "gzip": compress records before Firehose
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

# signal -> (protobuf request type, top-level key, scope list key, item list key)
SIGNALS = {
    "logs": (ExportLogsServiceRequest, "resourceLogs", "scopeLogs", "logRecords"),
    "traces": (ExportTraceServiceRequest, "resourceSpans", "scopeSpans", "spans"),
    "metrics": (ExportMetricsServiceRequest, "resourceMetrics", "scopeMetrics", "metrics"),
}
# Firehose records max out at 1,000 KiB; keep headroom for the JSON wrapping.
MAX_RECORD_BYTES = 1000 * 1024 - 4096
MAX_BATCH_RECORDS = 500
MAX_BATCH_BYTES = 4 * 1024 * 1024 - 64 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
TRUNCATED_BODY_BYTES = 256 * 1024
_ID_KEYS = {"traceId", "spanId", "parentSpanId"}

firehose = boto3.client("firehose")
ddb = boto3.client("dynamodb")


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def handler(event, context):
    try:
        tenant = ((event.get("requestContext") or {}).get("authorizer") or {}).get("tenant")
        if not tenant or not _TENANT.match(tenant):
            raise HttpError(401, "no tenant for this API key")
        signal = (event.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
        if signal == "cloudwatch-metrics":
            return firehose_handler(event, tenant)
        if signal not in SIGNALS:
            raise HttpError(404, f"unknown signal path {event.get('path')!r}")
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        ctype = (headers.get("content-type") or "application/x-protobuf").split(";")[0].strip().lower()

        ended = meter.trial_ended(tenant)
        if ended:
            meter.add(tenant, 0, 0, refused=len(event.get("body") or ""))
            print(json.dumps({"tenant": tenant, "signal": signal, "refused": "trial ended", "trial_ended_at": ended}))
            # 403: not retryable, so exporters drop the data instead of retrying it forever.
            return _response(403, "application/json",
                             f"this Leasyd free trial ended on {ended[:10]}; data is no longer accepted. "
                             f"To keep sending, contact {CONTACT_EMAIL}")
        cap = meter.over_cap(tenant)
        if cap:
            meter.add(tenant, 0, 0, refused=len(event.get("body") or ""))
            wait = 86400 - int(time.time()) % 86400
            print(json.dumps({"tenant": tenant, "signal": signal, "refused": "daily cap", "cap_bytes": cap}))
            out = _response(429, "application/json",
                            f"daily data limit reached ({_size(cap)} a day for this account); data is "
                            "accepted again from 00:00 UTC, or raise the limit in Leasyd")
            out["headers"]["Retry-After"] = str(wait)
            return out
        doc = parse(signal, _body(event, headers), ctype)
        out = accept(tenant, signal, doc, drop=True)
        print(json.dumps({"tenant": tenant, "signal": signal, **out}))
        return _response(200, ctype, None)
    except HttpError as e:
        print(json.dumps({"status": e.status, "error": str(e)}))
        return _response(e.status, "application/json", str(e))


def accept(tenant, signal, doc, drop=False):
    """Applies the tenant's drop rules (drop=True), puts what is left of an OTLP JSON document on the
    tenant's stream and meters it: items received (billed for ingest) and dropped (never stored)."""
    received, dropped = droprules.apply(signal, doc, meter.drop_rules(tenant)) if drop else (_count(signal, doc), 0)
    records = list(to_records(signal, doc))
    raw_bytes = sum(len(r) for r in records)
    if RECORD_COMPRESSION == "gzip":
        # Firehose bills the bytes it receives: send each record gzipped (~5x
        # smaller). Its S3 objects are then gzip members back to back, which
        # read as one gzip stream; the tenant's streams pass them through.
        records = [gzip.compress(r, compresslevel=6) for r in records]
    if records:
        put_records(f"{STREAM_PREFIX}{tenant}-{signal}", records)
    meter.add(tenant, raw_bytes, received, signal=signal, dropped=dropped)
    return {"records": len(records), "bytes": raw_bytes, "sent_bytes": sum(len(r) for r in records),
            "items": received, "dropped": dropped}


def firehose_handler(event, tenant):
    """POST /v1/aws/cloudwatch-metrics from the customer's Firehose (HTTP endpoint destination): answers
    as Firehose expects, {requestId, timestamp[, errorMessage]}; on an error Firehose retries, then keeps
    the data in the customer's backup bucket."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    request_id = headers.get("x-amz-firehose-request-id", "")

    def answer(status, error=None):
        body = {"requestId": request_id, "timestamp": int(time.time() * 1000)}
        if error:
            body["errorMessage"] = error
            print(json.dumps({"status": status, "error": error, "source": "cloudwatch"}))
        return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}

    ended = meter.trial_ended(tenant)
    if ended:
        return answer(403, f"this Leasyd free trial ended on {ended[:10]}; contact {CONTACT_EMAIL}")
    if meter.over_cap(tenant):
        return answer(429, "daily data limit reached; accepted again from 00:00 UTC")
    try:
        rid, lines = cloudwatch.firehose_lines(_body(event, headers))
        request_id = request_id or rid
        doc = cloudwatch.to_otlp(lines)
        out = accept(tenant, "metrics", doc, drop=True) if doc["resourceMetrics"] else {"records": 0}
    except cloudwatch.BadRequest as e:
        return answer(400, str(e))
    except HttpError as e:
        return answer(e.status, str(e))
    print(json.dumps({"tenant": tenant, "signal": "metrics", "source": "cloudwatch", "lines": len(lines), **out}))
    return answer(200)


# ------------------------------------------------------------------ metering and daily caps

class Meter:
    """Per container: what was accepted per (tenant, UTC day), added to obs-tenants'
    meter#<tenant>#<day> every METER_FLUSH_S: bytes (stored), records (received, all signals),
    refused_bytes, and per signal in_<signal> (received) and dropped_<signal> (by drop rules).
    Each tenant's cap and usage are re-read every CAP_CHECK_S (so a cap is enforced within about
    that, plus the other containers' unflushed counts), and its drop rules (drop#<tenant>) likewise."""

    def __init__(self):
        self.pending, self.caps, self.rules, self.flushed_at = {}, {}, {}, time.time()

    def drop_rules(self, tenant):
        """The tenant's drop rules (a list), re-read every CAP_CHECK_S; none if unreadable."""
        if not TENANTS_TABLE:
            return []
        now = time.time()
        cached = self.rules.get(tenant)
        if not cached or now - cached[0] > CAP_CHECK_S:
            try:
                item = ddb.get_item(TableName=TENANTS_TABLE, Key={"pk": {"S": f"drop#{tenant}"}},
                                    ProjectionExpression="rules_json").get("Item") or {}
                rules = json.loads(item.get("rules_json", {}).get("S", "[]"))
            except Exception as e:   # never fail ingest on rules: keep everything
                print(json.dumps({"drop_rules": "read failed", "error": str(e)[:200]}))
                rules = cached[1] if cached else []
            cached = self.rules[tenant] = (now, rules if isinstance(rules, list) else [])
        return cached[1]

    def trial_ended(self, tenant):
        """-> when the tenant's free trial ended (ISO-8601), if it has; else None."""
        self.over_cap(tenant)   # reads (or reuses) the tenant record
        c = self.caps.get(tenant)
        end = c[4] if c else None
        return end if end and end <= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) else None

    def over_cap(self, tenant):
        """-> the tenant's daily cap in bytes if today's usage has reached it, else None."""
        if not TENANTS_TABLE:
            return None
        day, now = _day(), time.time()
        c = self.caps.get(tenant)
        if not c or c[1] != day or now - c[0] > CAP_CHECK_S:
            try:
                rec = ddb.get_item(TableName=TENANTS_TABLE, Key={"pk": {"S": f"tenant#{tenant}"}},
                                   ProjectionExpression="daily_cap_bytes, trial_ends_at").get("Item") or {}
                cap = int(rec["daily_cap_bytes"]["N"]) if "daily_cap_bytes" in rec else None
                trial_end = rec.get("trial_ends_at", {}).get("S")
                used = 0
                if cap:
                    m = ddb.get_item(TableName=TENANTS_TABLE, Key={"pk": {"S": f"meter#{tenant}#{day}"}},
                                     ProjectionExpression="#b", ExpressionAttributeNames={"#b": "bytes"}).get("Item") or {}
                    used = int(m.get("bytes", {}).get("N", 0))
            except Exception as e:   # never fail ingest on metering
                print(json.dumps({"meter": "cap check failed", "error": str(e)[:200]}))
                return None
            c = self.caps[tenant] = (now, day, cap, used, trial_end)
        _, _, cap, used, _ = c
        pending = self.pending.get((tenant, day), {}).get("bytes", 0)
        return cap if cap is not None and used + pending >= cap else None

    def add(self, tenant, nbytes, records, refused=0, signal=None, dropped=0):
        if not TENANTS_TABLE:
            return
        p = self.pending.setdefault((tenant, _day()), {})
        counts = {"bytes": nbytes, "records": records, "refused_bytes": refused}
        if signal:
            counts.update({f"in_{signal}": records, f"dropped_{signal}": dropped})
        for k, v in counts.items():
            p[k] = p.get(k, 0) + v
        if time.time() - self.flushed_at >= METER_FLUSH_S:
            self.flush()

    def flush(self):
        self.flushed_at = time.time()
        for (tenant, day), counts in list(self.pending.items()):
            names = sorted(counts)
            try:
                ddb.update_item(TableName=TENANTS_TABLE, Key={"pk": {"S": f"meter#{tenant}#{day}"}},
                                UpdateExpression="ADD " + ", ".join(f"#c{i} :c{i}" for i in range(len(names)))
                                                 + " SET tenant = :t, #d = :d",
                                ExpressionAttributeNames={"#d": "day", **{f"#c{i}": n for i, n in enumerate(names)}},
                                ExpressionAttributeValues={":t": {"S": tenant}, ":d": {"S": day},
                                                           **{f":c{i}": {"N": str(counts[n])} for i, n in enumerate(names)}})
            except Exception as e:   # keep the counts for the next flush
                print(json.dumps({"meter": "flush failed", "error": str(e)[:200]}))
                continue
            del self.pending[(tenant, day)]
            c = self.caps.get(tenant)
            if c and c[1] == day:
                self.caps[tenant] = (c[0], day, c[2], c[3] + counts.get("bytes", 0), c[4])


def _size(n):
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:g} {unit}"
    return f"{n} bytes"


def _day():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _count(signal, doc):
    """Log records, spans or metric data points in an OTLP JSON document."""
    _, top, scope_key, item_key = SIGNALS[signal]
    n = 0
    for res in doc.get(top) or []:
        for scope in res.get(scope_key) or []:
            for item in scope.get(item_key) or []:
                if signal == "metrics":
                    for kind in ("gauge", "sum", "histogram", "exponentialHistogram", "summary"):
                        n += len((item.get(kind) or {}).get("dataPoints") or [])
                else:
                    n += 1
    return n


meter = Meter()


# ------------------------------------------------------------------ parsing

def _body(event, headers):
    raw = event.get("body") or ""
    data = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    # API Gateway may already have decompressed a gzip body (keeping the
    # Content-Encoding header), so trust the gzip magic bytes, not the header.
    if headers.get("content-encoding", "").lower() == "gzip" and data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
                data = f.read(MAX_DECOMPRESSED_BYTES + 1)
        except (OSError, EOFError) as e:
            raise HttpError(400, f"bad gzip body: {e}")
        if len(data) > MAX_DECOMPRESSED_BYTES:
            raise HttpError(413, "decompressed body too large")
    return data


def parse(signal, body, ctype):
    """OTLP request -> OTLP JSON dict (ids as hex, enums as integers)."""
    cls, top, _, _ = SIGNALS[signal]
    if ctype == "application/x-protobuf":
        msg = cls()
        try:
            msg.ParseFromString(body)
        except DecodeError as e:
            raise HttpError(400, f"bad protobuf body: {e}")
        doc = json_format.MessageToDict(msg, use_integers_for_enums=True)
        _ids_to_hex(doc)
    elif ctype == "application/json":
        try:
            doc = json.loads(body)
        except ValueError as e:
            raise HttpError(400, f"bad JSON body: {e}")
        if not isinstance(doc, dict):
            raise HttpError(400, "JSON body must be an object")
    else:
        raise HttpError(415, f"unsupported content type {ctype!r}")
    resources = doc.get(top) or []
    for r in resources:
        _strip_reserved(r)
    return {top: resources}


def _ids_to_hex(node):
    """Protobuf JSON encodes bytes as base64; OTLP JSON uses hex for ids."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _ID_KEYS and isinstance(v, str):
                node[k] = base64.b64decode(v).hex()
            else:
                _ids_to_hex(v)
    elif isinstance(node, list):
        for v in node:
            _ids_to_hex(v)


def _strip_reserved(resource_entry):
    """obs.* resource attributes are the platform's own; clients can't set them."""
    res = resource_entry.get("resource")
    if isinstance(res, dict) and isinstance(res.get("attributes"), list):
        res["attributes"] = [a for a in res["attributes"]
                             if not str(a.get("key", "")).startswith("obs.")]


# ------------------------------------------------------------ record sizing

def _line(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def to_records(signal, doc):
    """One newline-terminated OTLP JSON document per record, each under
    Firehose's record limit: the whole request if it fits, else split by
    resource, then by batches of items within each scope."""
    _, top, scope_key, item_key = SIGNALS[signal]
    whole = _line(doc)
    if len(whole) <= MAX_RECORD_BYTES:
        if doc[top]:
            yield whole
        return
    for resource in doc[top]:
        one = _line({top: [resource]})
        if len(one) <= MAX_RECORD_BYTES:
            yield one
            continue
        shell = {k: v for k, v in resource.items() if k != scope_key}
        for scope in resource.get(scope_key) or []:
            scope_shell = {k: v for k, v in scope.items() if k != item_key}
            base = len(_line({top: [{**shell, scope_key: [{**scope_shell, item_key: []}]}]}))
            batch, size = [], base
            for item in scope.get(item_key) or []:
                item = _fit(signal, item, MAX_RECORD_BYTES - base)
                if item is None:
                    continue
                n = len(json.dumps(item, separators=(",", ":"))) + 1
                if batch and size + n > MAX_RECORD_BYTES:
                    yield _line({top: [{**shell, scope_key: [{**scope_shell, item_key: batch}]}]})
                    batch, size = [], base
                batch.append(item)
                size += n
            if batch:
                yield _line({top: [{**shell, scope_key: [{**scope_shell, item_key: batch}]}]})


def _fit(signal, item, room):
    """A single item bigger than a record: truncate a log body, else drop it."""
    if len(json.dumps(item, separators=(",", ":"))) <= room:
        return item
    if signal == "logs" and isinstance(item.get("body"), dict) and "stringValue" in item["body"]:
        item = dict(item)
        item["body"] = {"stringValue": item["body"]["stringValue"][:TRUNCATED_BODY_BYTES]}
        item["attributes"] = list(item.get("attributes") or []) + [
            {"key": "log.body.truncated", "value": {"boolValue": True}}]
        if len(json.dumps(item, separators=(",", ":"))) <= room:
            return item
    print(json.dumps({"dropped_oversized_item": signal}))
    return None


# ------------------------------------------------------------------ Firehose

def put_records(stream, records):
    """PutRecordBatch in chunks, retrying records Firehose reports as failed."""
    for chunk in _chunks(records):
        pending = chunk
        for attempt in range(5):
            try:
                resp = firehose.put_record_batch(DeliveryStreamName=stream,
                                                 Records=[{"Data": r} for r in pending])
            except firehose.exceptions.ResourceNotFoundException:
                raise HttpError(503, "tenant ingest stream not provisioned")
            except firehose.exceptions.ResourceInUseException:
                raise HttpError(503, "tenant ingest stream not ready yet; retry")
            except firehose.exceptions.ServiceUnavailableException:
                resp = {"FailedPutCount": len(pending),
                        "RequestResponses": [{"ErrorCode": "ServiceUnavailable"}] * len(pending)}
            if not resp.get("FailedPutCount"):
                break
            pending = [r for r, res in zip(pending, resp["RequestResponses"]) if res.get("ErrorCode")]
            time.sleep(min(0.1 * 2 ** attempt, 1.0))
        else:
            raise HttpError(503, f"{len(pending)} records not accepted by Firehose; retry")


def _chunks(records):
    batch, size = [], 0
    for r in records:
        if batch and (len(batch) >= MAX_BATCH_RECORDS or size + len(r) > MAX_BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(r)
        size += len(r)
    if batch:
        yield batch


def _response(status, ctype, error):
    if status == 200 and ctype == "application/x-protobuf":
        # An empty Export*ServiceResponse serialises to zero bytes.
        return {"statusCode": 200, "headers": {"Content-Type": ctype}, "body": "", "isBase64Encoded": True}
    body = "{}" if status == 200 else json.dumps({"message": error})
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": body}
