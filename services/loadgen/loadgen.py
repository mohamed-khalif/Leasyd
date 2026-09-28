"""Load generator and freshness prober for the T6 scale tests.

Runs as the obs-loadgen Lambda, driven by infra/t6/loadtest.py.

mode "send"  {run, step, worker, endpoint, duration_s, mix, tenants: [{tenant, key, bytes_per_s, services}]}
    Sends realistic OTLP/HTTP protobuf (gzip, like the SDKs' exporters) to
    the public endpoint at each tenant's rate, for duration_s. Retries 429
    and 5xx with backoff like an SDK. Writes one summary item to the
    obs-loadtest table: bytes and requests sent, HTTP statuses, retries,
    dropped batches, request latency percentiles, and how far behind its
    target rate it fell (to tell a slow generator from a slow platform).

mode "probe" {run, step, endpoint, duration_s, interval_s, tenants: [{tenant, key}]}
    Every interval, per tenant: sends one log record with a new trace id,
    then polls obs-index-lookup for that id until found (freshness). Also
    times a 1-hour and a 24-hour lookup, with and without an ID. One item
    per measurement in obs-loadtest.

Volume is counted as uncompressed OTLP protobuf bytes, the usual measure of
telemetry volume.
"""

import gzip
import heapq
import http.client
import json
import os
import random
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import boto3
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    AggregationTemporality, Gauge, Histogram, HistogramDataPoint, Metric, NumberDataPoint, ResourceMetrics,
    ScopeMetrics, Sum)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

TABLE = os.environ.get("LOADTEST_TABLE", "obs-loadtest")
LOOKUP = os.environ.get("LOOKUP_FUNCTION", "obs-index-lookup")
BATCH_ITEMS = 512          # the SDKs' default max export batch size
HTTP_THREADS = 16
MAX_RETRIES = 5

ddb = boto3.client("dynamodb")
lam = boto3.client("lambda")


def handler(event, context):
    return {"send": send, "probe": probe}[event["mode"]](event, context)


# ------------------------------------------------------------ payloads

ROUTES = ["/api/orders", "/api/cart", "/api/users/{id}", "/api/search", "/health", "/api/payments", "/api/items/{id}"]
METHODS = ["GET", "GET", "GET", "POST", "PUT", "DELETE"]
LEVELS = [(9, "INFO")] * 8 + [(13, "WARN"), (17, "ERROR")]
WORDS = ("request handled user order cart payment timeout retry cache miss hit db query slow upstream "
         "connection reset accepted rejected validation failed success started finished").split()


def kv(k, v):
    if isinstance(v, bool):
        return KeyValue(key=k, value=AnyValue(bool_value=v))
    if isinstance(v, int):
        return KeyValue(key=k, value=AnyValue(int_value=v))
    return KeyValue(key=k, value=AnyValue(string_value=v))


def resource(tenant, service):
    return Resource(attributes=[
        kv("service.name", service), kv("service.version", "1.4.2"), kv("deployment.environment", "prod"),
        kv("host.name", f"{service}-{random.randint(1, 8)}"), kv("cloud.region", "us-east-1"),
        kv("telemetry.sdk.language", "python"), kv("telemetry.sdk.name", "opentelemetry"),
    ])


def logs_request(tenant, service, now_ns):
    recs = []
    for i in range(BATCH_ITEMS):
        sev, sev_text = random.choice(LEVELS)
        route = random.choice(ROUTES)
        recs.append(LogRecord(
            time_unix_nano=now_ns - random.randint(0, 2_000_000_000), observed_time_unix_nano=now_ns,
            severity_number=sev, severity_text=sev_text,
            body=AnyValue(string_value=" ".join(random.choices(WORDS, k=random.randint(8, 30)))),
            trace_id=secrets.token_bytes(16), span_id=secrets.token_bytes(8),
            attributes=[kv("http.method", random.choice(METHODS)), kv("http.route", route),
                        kv("http.status_code", 500 if sev == 17 else 200),
                        kv("request.id", secrets.token_hex(8)), kv("user.id", f"u{random.randint(1, 50000)}"),
                        kv("duration_ms", random.randint(1, 900))]))
    return ExportLogsServiceRequest(resource_logs=[ResourceLogs(
        resource=resource(tenant, service),
        scope_logs=[ScopeLogs(scope=InstrumentationScope(name="app.logger"), log_records=recs)])])


def traces_request(tenant, service, now_ns):
    spans = []
    while len(spans) < BATCH_ITEMS:
        trace_id, parent = secrets.token_bytes(16), b""
        for depth in range(random.randint(1, 6)):
            sid = secrets.token_bytes(8)
            start = now_ns - random.randint(0, 2_000_000_000)
            dur = random.randint(100_000, 800_000_000)
            spans.append(Span(
                trace_id=trace_id, span_id=sid, parent_span_id=parent,
                name=f"{random.choice(METHODS)} {random.choice(ROUTES)}" if depth == 0 else f"db.query {depth}",
                kind=Span.SPAN_KIND_SERVER if depth == 0 else Span.SPAN_KIND_CLIENT,
                start_time_unix_nano=start, end_time_unix_nano=start + dur,
                status=Status(code=Status.STATUS_CODE_ERROR if random.random() < 0.02 else Status.STATUS_CODE_OK),
                attributes=[kv("http.route", random.choice(ROUTES)), kv("http.status_code", 200),
                            kv("request.id", secrets.token_hex(8)), kv("db.system", "postgresql"),
                            kv("net.peer.name", "db-1")],
                events=[Span.Event(time_unix_nano=start + dur // 2, name="retry")] if random.random() < 0.05 else []))
            parent = sid
    return ExportTraceServiceRequest(resource_spans=[ResourceSpans(
        resource=resource(tenant, service),
        scope_spans=[ScopeSpans(scope=InstrumentationScope(name="app.tracer"), spans=spans[:BATCH_ITEMS])])])


def metrics_request(tenant, service, now_ns):
    metrics = []
    for m in range(40):
        pts_attrs = [[kv("http.route", r), kv("http.method", "GET")] for r in ROUTES]
        if m % 3 == 0:
            metrics.append(Metric(name=f"http.server.duration.{m}", unit="ms", histogram=Histogram(
                aggregation_temporality=AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA,
                data_points=[HistogramDataPoint(
                    attributes=a, start_time_unix_nano=now_ns - 60_000_000_000, time_unix_nano=now_ns,
                    count=100, sum=random.random() * 1e4, bucket_counts=[random.randint(0, 30) for _ in range(11)],
                    explicit_bounds=[5, 10, 25, 50, 75, 100, 250, 500, 750, 1000]) for a in pts_attrs])))
        elif m % 3 == 1:
            metrics.append(Metric(name=f"http.server.requests.{m}", sum=Sum(
                aggregation_temporality=AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE, is_monotonic=True,
                data_points=[NumberDataPoint(attributes=a, start_time_unix_nano=now_ns - 3_600_000_000_000,
                                             time_unix_nano=now_ns, as_int=random.randint(0, 10**6))
                             for a in pts_attrs])))
        else:
            metrics.append(Metric(name=f"process.cpu.{m}", unit="1", gauge=Gauge(data_points=[
                NumberDataPoint(attributes=a, time_unix_nano=now_ns, as_double=random.random()) for a in pts_attrs])))
    return ExportMetricsServiceRequest(resource_metrics=[ResourceMetrics(
        resource=resource(tenant, service),
        scope_metrics=[ScopeMetrics(scope=InstrumentationScope(name="app.meter"), metrics=metrics)])])


BUILDERS = {"logs": logs_request, "traces": traces_request, "metrics": metrics_request}


# ------------------------------------------------------------------ HTTP

class Poster:
    """One keep-alive HTTPS connection per thread, like an SDK exporter."""

    def __init__(self, endpoint):
        u = urllib.parse.urlparse(endpoint)
        self.host, self.base = u.netloc, u.path.rstrip("/")
        self.local = threading.local()

    def _conn(self, fresh=False):
        if fresh or getattr(self.local, "conn", None) is None:
            self.local.conn = http.client.HTTPSConnection(self.host, timeout=30)
        return self.local.conn

    def post(self, key, signal, body):
        """-> (final status, attempts, seconds). Retries 429/5xx and network errors."""
        t0 = time.time()
        status = 0
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                c = self._conn()
                c.request("POST", f"{self.base}/v1/{signal}", body=body, headers={
                    "x-api-key": key, "Content-Type": "application/x-protobuf", "Content-Encoding": "gzip"})
                r = c.getresponse()
                r.read()
                status = r.status
            except (OSError, http.client.HTTPException):
                status = 0
                self._conn(fresh=True)
            if status == 200 or (400 <= status < 500 and status != 429):
                return status, attempt, time.time() - t0
            time.sleep(min(0.5 * 2 ** (attempt - 1), 8) * (0.5 + random.random()))
        return status, MAX_RETRIES + 1, time.time() - t0


# ------------------------------------------------------------------ send

def send(event, context):
    end = time.time() + float(event["duration_s"])
    mix = event.get("mix") or {"logs": 0.6, "traces": 0.3, "metrics": 0.1}
    poster = Poster(event["endpoint"])
    stats = {"bytes": 0, "gz_bytes": 0, "requests": 0, "retries": 0, "dropped": 0, "status": {}, "lat": []}
    lock = threading.Lock()

    def record(nbytes, gz, result):
        status, attempts, secs = result
        with lock:
            stats["requests"] += 1
            stats["retries"] += attempts - 1
            stats["status"][str(status)] = stats["status"].get(str(status), 0) + 1
            stats["lat"].append(secs)
            if status == 200:
                stats["bytes"] += nbytes
                stats["gz_bytes"] += gz
            else:
                stats["dropped"] += 1

    # Each tenant sends its next batch when its byte budget allows it.
    heap = [(time.time() + random.random() * 2, i) for i in range(len(event["tenants"]))]
    sent_by = [0] * len(event["tenants"])
    start = time.time()
    behind = 0.0
    pending = []
    with ThreadPoolExecutor(HTTP_THREADS) as pool:
        while heap:
            due, i = heapq.heappop(heap)
            if due >= end:
                continue
            wait = due - time.time()
            if wait > 0:
                time.sleep(wait)
            else:
                behind = max(behind, -wait)
            t = event["tenants"][i]
            signal = random.choices(list(mix), weights=list(mix.values()))[0]
            msg = BUILDERS[signal](t["tenant"], random.choice(t["services"]), time.time_ns())
            raw = msg.SerializeToString()
            body = gzip.compress(raw, compresslevel=6)
            pending.append(pool.submit(lambda b=body, k=t["key"], s=signal, n=len(raw), g=len(body):
                                       record(n, g, poster.post(k, s, b))))
            pending = [f for f in pending if not f.done()]
            while len(pending) > HTTP_THREADS * 4:   # don't queue unboundedly if the platform slows down
                time.sleep(0.05)
                pending = [f for f in pending if not f.done()]
            sent_by[i] += len(raw)
            heapq.heappush(heap, (start + sent_by[i] / t["bytes_per_s"], i))

    elapsed = time.time() - start
    lat = sorted(stats["lat"]) or [0]
    target = sum(t["bytes_per_s"] for t in event["tenants"])
    item = {
        "pk": {"S": f"{event['run']}#{event['step']}"}, "sk": {"S": f"send#{event['worker']}#{int(start)}"},
        "tenants": {"N": str(len(event["tenants"]))}, "elapsed_s": {"N": f"{elapsed:.1f}"},
        "target_bytes_per_s": {"N": f"{target:.0f}"}, "bytes": {"N": str(stats["bytes"])},
        "gz_bytes": {"N": str(stats["gz_bytes"])}, "requests": {"N": str(stats["requests"])},
        "retries": {"N": str(stats["retries"])}, "dropped": {"N": str(stats["dropped"])},
        "status": {"S": json.dumps(stats["status"])}, "max_behind_s": {"N": f"{behind:.1f}"},
        "lat_p50": {"N": f"{lat[len(lat) // 2]:.3f}"}, "lat_p99": {"N": f"{lat[int(len(lat) * 0.99)]:.3f}"},
    }
    ddb.put_item(TableName=TABLE, Item=item)
    return {k: list(v.values())[0] for k, v in item.items()}


# ----------------------------------------------------------------- probe

def _lookup(payload):
    t0 = time.time()
    r = lam.invoke(FunctionName=LOOKUP, Payload=json.dumps(payload).encode())
    out = json.loads(r["Payload"].read())
    return out, time.time() - t0


def _epoch(iso):
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _probe_once(event, t, poster):
    trace = secrets.token_hex(16)
    now_ns = time.time_ns()
    req = ExportLogsServiceRequest(resource_logs=[ResourceLogs(
        resource=Resource(attributes=[kv("service.name", "t6-probe")]),
        scope_logs=[ScopeLogs(log_records=[LogRecord(
            time_unix_nano=now_ns, body=AnyValue(string_value="probe"), trace_id=bytes.fromhex(trace))])])])
    status, _, _ = poster.post(t["key"], "logs", gzip.compress(req.SerializeToString()))
    sent = time.time()
    item = {"pk": {"S": f"{event['run']}#{event['step']}"}, "sk": {"S": f"probe#{t['tenant']}#{int(sent)}"},
            "tenant": {"S": t["tenant"]}, "send_status": {"N": str(status)}}
    found = None
    if status == 200:
        q = {"tenant": t["tenant"], "signal": "logs", "services": ["t6-probe"], "start": _iso(sent - 600),
             "end": _iso(sent + 600), "match": {"trace_id": trace}}
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now_ns // 10**9))
        while time.time() - sent < 300:
            out, _ = _lookup(q)
            # A file whose time range covers the probe record (a bloom false
            # positive on another file must not count as found).
            hit = [f for f in out.get("files", []) if f["min_ts"][:19] <= ts <= f["max_ts"][:19]]
            if hit:
                found = time.time() - sent
                f = hit[0]
                # Where the time went: Firehose buffer + delivery, S3 event ->
                # fast lane start, parse, then upload + index + lookup polling.
                marks = [sent] + [_epoch(f.get(k)) for k in ("delivered_at", "received_at", "indexed_at")]
                if None not in marks:
                    for name, a, b in (("firehose_s", 0, 1), ("trigger_s", 1, 2), ("parse_s", 2, 3)):
                        item[name] = {"N": f"{marks[b] - marks[a]:.1f}"}
                    item["visible_s"] = {"N": f"{sent + found - marks[3]:.1f}"}
                break
            time.sleep(2)
    item["freshness_s"] = {"N": f"{found:.1f}" if found is not None else "-1"}
    # Lookup speed across the tenant's services: 1 h and 24 h, with and without an ID.
    for label, hours, match in (("1h", 1, None), ("24h", 24, None), ("24h_id", 24, {"trace_id": secrets.token_hex(16)})):
        q = {"tenant": t["tenant"], "signal": "logs", "start": _iso(sent - hours * 3600), "end": _iso(sent)}
        if match:
            q["match"] = match
        out, secs = _lookup(q)
        st = out.get("stats", {})
        item[f"lookup_{label}_s"] = {"N": f"{secs:.3f}"}
        item[f"lookup_{label}_files"] = {"N": str(len(out.get("files", [])))}
        item[f"lookup_{label}_candidates"] = {"N": str(st.get("in_time_range", 0))}
        item[f"lookup_{label}_rcu"] = {"N": f"{st.get('read_units', 0):.1f}"}
    ddb.put_item(TableName=TABLE, Item=item)


def probe(event, context):
    end = time.time() + float(event["duration_s"])
    poster = Poster(event["endpoint"])
    with ThreadPoolExecutor(len(event["tenants"])) as pool:
        while time.time() < end - 300:   # leave time for the last probe to be found
            t0 = time.time()
            list(pool.map(lambda t: _probe_once(event, t, poster), event["tenants"]))
            time.sleep(max(0, float(event.get("interval_s", 60)) - (time.time() - t0)))
    return {"ok": True}
