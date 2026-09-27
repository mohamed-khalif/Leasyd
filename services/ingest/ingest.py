"""OTLP/HTTP ingest Lambda (behind API Gateway and the API-key authorizer).

POST /v1/logs | /v1/traces | /v1/metrics, body OTLP protobuf or JSON,
optionally gzip-encoded. The request is converted to one OTLP JSON line per
Firehose record and put on the tenant's own delivery stream
(obs-t-<tenant>-<signal>), which batches it into
s3://.../_incoming/tenant=<T>/<signal>/dt=/hour=/ for compaction.

- Tenant: only from the authorizer's context (i.e. from the API key). Nothing
  the client sends can choose it; obs.* resource attributes from the client
  are stripped.
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

STREAM_PREFIX = os.environ.get("STREAM_PREFIX", "obs-t-")
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
        if signal not in SIGNALS:
            raise HttpError(404, f"unknown signal path {event.get('path')!r}")
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        ctype = (headers.get("content-type") or "application/x-protobuf").split(";")[0].strip().lower()

        doc = parse(signal, _body(event, headers), ctype)
        records = list(to_records(signal, doc))
        put_records(f"{STREAM_PREFIX}{tenant}-{signal}", records)
        print(json.dumps({"tenant": tenant, "signal": signal, "records": len(records),
                          "bytes": sum(len(r) for r in records)}))
        return _response(200, ctype, None)
    except HttpError as e:
        print(json.dumps({"status": e.status, "error": str(e)}))
        return _response(e.status, "application/json", str(e))


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
