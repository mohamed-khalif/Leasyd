"""Demo tenant: an online shop that sends realistic telemetry, the way a customer's apps would.

Every minute (scheduled) it sends the previous minute's traces, logs and metrics for the
"leasyd-demo" tenant through the real ingest endpoint. Everything is derived from the minute
(seeded randomness), so backfilled history and live minutes tell one consistent story:

  - a dozen services (frontend-proxy, frontend, checkout, cart, payment, shipping, ...), two pods
    each, calling each other: one trace per customer request, 3-20 spans across services
  - traffic that follows the day (busiest ~15:00 UTC, quietest ~03:00 UTC)
  - about 3% of payments declined (error spans, ERROR logs, a declined-payments counter)
  - a recurring incident: for 12 minutes every 3 hours the shipping service slows ~10x and
    some quotes time out, checkouts fail, CPU climbs and the order queue backs up
  - logs tied to their spans (trace_id / span_id), metrics of every kind: request-duration
    histograms, a cumulative orders counter (pods restart every 6 hours: it resets), CPU and
    queue-lag gauges, memory and connection-pool up/down counters

Invoke with {} (scheduled: the previous minute), {"backfill_hours": 24} (fans out one async
invocation per hour) or {"from_minute": m, "minutes": 60}. The ingest key is read from SSM.
"""

import gzip
import hashlib
import json
import math
import os
import random
import time
import urllib.error
import urllib.request

import boto3

ENDPOINT = os.environ.get("INGEST_ENDPOINT", "")
KEY_PARAM = os.environ.get("KEY_PARAM", "/obs/demo/api-key")
SELF = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "obs-demo")
PEAK_REQUESTS_PER_MINUTE = int(os.environ.get("PEAK_REQUESTS_PER_MINUTE", "120"))
MS = 1_000_000  # ns

# service -> (language, version, own time per call in ms)
SERVICES = {
    "frontend-proxy": ("cpp", "1.28.0", 1.5), "frontend": ("nodejs", "2.4.1", 6),
    "product-catalog": ("go", "1.9.3", 4), "cart": ("dotnet", "3.2.0", 3), "checkout": ("go", "2.0.7", 8),
    "currency": ("cpp", "1.3.2", 1), "shipping": ("rust", "1.6.0", 12), "payment": ("nodejs", "1.12.4", 35),
    "email": ("ruby", "1.4.0", 20), "recommendation": ("python", "1.8.2", 15),
    "accounting": ("dotnet", "1.1.5", 9), "fraud-detection": ("java", "1.2.0", 14),
}
PODS = {s: [f"{s}-{hashlib.md5(s.encode()).hexdigest()[:9]}-{hashlib.md5(f'{s}{i}'.encode()).hexdigest()[:5]}"
            for i in range(2)] for s in SERVICES}
NODES = ["ip-10-0-1-37.ec2.internal", "ip-10-0-2-114.ec2.internal", "ip-10-0-3-9.ec2.internal"]
PRODUCTS = ["OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "L9ECAV7KIM", "2ZYFJ3GM2N", "0PUK6V6EV0", "LS4PSXUNUM", "9SIQT8TOJO"]
BOUNDS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000]   # request-duration histogram buckets (ms)
INCIDENT_EVERY, INCIDENT_FOR = 180, 12                       # minutes


def load(minute):
    """0.35 (03:00 UTC) .. 1.0 (15:00 UTC)."""
    hour = (minute % 1440) / 60
    return 0.35 + 0.65 * (0.5 + 0.5 * math.cos(2 * math.pi * (hour - 15) / 24))


def incident(minute):
    return minute % INCIDENT_EVERY < INCIDENT_FOR


# ------------------------------------------------------------------ traces and logs

class Minute:
    """Everything one minute of the shop produces, grouped by (service, pod) resource."""

    def __init__(self, minute):
        self.minute, self.rnd = minute, random.Random(minute)
        self.spans, self.logs = {}, {}       # (service, pod) -> list
        self.requests = {}                   # (service, pod, route) -> [durations ms]
        self.declines = {}                   # pod -> count
        for _ in range(round(PEAK_REQUESTS_PER_MINUTE * load(minute) * (0.9 + 0.2 * self.rnd.random()))):
            self.request()

    def id(self, n):
        return "%0*x" % (n, self.rnd.getrandbits(n * 4))

    def request(self):
        r, t0 = self.rnd, (self.minute * 60 + self.rnd.random() * 58) * 10**9
        user, kind = f"u-{r.randint(1000, 9999)}", r.random()
        if kind < 0.45:
            route, inner = "GET /api/products", [self.call("product-catalog", "ListProducts"),
                                                 self.call("recommendation", "ListRecommendations",
                                                           [self.call("product-catalog", "ListProducts")])]
        elif kind < 0.7:
            p = r.choice(PRODUCTS)
            route, inner = "GET /api/products/{id}", [self.call("product-catalog", "GetProduct", attrs={"app.product.id": p}),
                                                      self.call("currency", "Convert")]
        elif kind < 0.9:
            route, inner = "POST /api/cart", [self.call("cart", "AddItem", [self.db("cart", "HSET")],
                                                        attrs={"app.product.id": r.choice(PRODUCTS)})]
        else:
            route, inner = "POST /api/checkout", [self.checkout()]
        tree = self.call("frontend-proxy", "ingress", [self.call("frontend", route, inner, kind="server")],
                         attrs={"http.request.method": route.split()[0], "http.route": route.split()[1]})
        self.emit(tree, self.id(32), None, t0, {"user.id": user})

    def call(self, service, name, children=(), kind="server", attrs=None, error=None, slow=1.0):
        return {"service": service, "name": name, "children": list(children), "kind": kind,
                "attrs": attrs or {}, "error": error, "slow": slow}

    def db(self, service, op):
        return self.call(service, op, kind="client", attrs={"db.system": "redis", "db.operation": op})

    def checkout(self):
        r, broken = self.rnd, incident(self.minute)
        order = self.id(12)
        ship_err = "shipping quote timed out after 5s" if broken and r.random() < 0.25 else None
        pay_err = None if ship_err or r.random() > 0.03 else r.choice(
            ["payment declined: card expired", "payment declined: insufficient funds", "payment declined: suspected fraud"])
        steps = [self.call("cart", "GetCart", [self.db("cart", "HGET")]),
                 *[self.call("product-catalog", "GetProduct") for _ in range(r.randint(1, 3))],
                 self.call("currency", "Convert"),
                 self.call("shipping", "GetQuote", error=ship_err, slow=(10 if broken else 1) * (40 if ship_err else 1))]
        if not ship_err:
            steps.append(self.call("payment", "Charge", error=pay_err,
                                   attrs={"app.payment.method": r.choice(["card", "card", "card", "paypal"])}))
        if not ship_err and not pay_err:
            steps += [self.call("shipping", "ShipOrder"), self.call("email", "SendOrderConfirmation"),
                      self.call("checkout", "orders publish", [self.call("accounting", "orders process", kind="consumer"),
                                                               self.call("fraud-detection", "orders process", kind="consumer")],
                                kind="producer"),
                      self.call("cart", "EmptyCart", [self.db("cart", "DEL")])]
        return self.call("checkout", "PlaceOrder", steps, attrs={"app.order.id": order},
                         error=ship_err or pay_err)

    def emit(self, node, trace, parent, start, ctx):
        r, svc = self.rnd, node["service"]
        pod = r.choice(PODS[svc])
        own = SERVICES[svc][2] * node["slow"] * math.exp(r.gauss(0, 0.45))
        span_id, t = self.id(16), start + own / 2 * MS
        for child in node["children"]:
            t = self.emit(child, trace, span_id, t, ctx) + r.uniform(0.05, 0.4) * MS
        end = t + own / 2 * MS
        dur = (end - start) / MS
        grpc = node["kind"] == "server" and node["name"][0].isupper() and " " not in node["name"]   # e.g. PlaceOrder
        attrs = {**node["attrs"], **({"rpc.system": "grpc", "rpc.method": node["name"]} if grpc else {})}
        span = {"traceId": trace, "spanId": span_id, "name": node["name"],
                "kind": {"server": 2, "client": 3, "producer": 4, "consumer": 5}[node["kind"]],
                "startTimeUnixNano": str(int(start)), "endTimeUnixNano": str(int(end)),
                "attributes": _attrs(attrs), "status": {"code": 2, "message": node["error"]} if node["error"] else {}}
        if parent:
            span["parentSpanId"] = parent
        self.spans.setdefault((svc, pod), []).append(span)
        if node["kind"] == "server":
            self.requests.setdefault((svc, pod, node["name"]), []).append(dur)
            self.log(svc, pod, trace, span_id, end, node, dur, ctx)
        if node["error"] and svc == "payment":
            self.declines[pod] = self.declines.get(pod, 0) + 1
        return end

    def log(self, svc, pod, trace, span, t, node, dur, ctx):
        r = self.rnd
        attrs = {k: v for k, v in {**ctx, **node["attrs"]}.items() if k.startswith(("app.", "user.", "http."))}
        if node["error"]:
            sev, body = (17, "ERROR"), f"{node['name']} failed: {node['error']}"
        elif dur > 1000:
            sev, body = (13, "WARN"), f"{node['name']} slow: {dur:.0f} ms"
        elif svc == "product-catalog" and r.random() < 0.3:
            sev, body = (5, "DEBUG"), f"cache hit for product {r.choice(PRODUCTS)}"
        elif r.random() < 0.4:
            sev, body = (9, "INFO"), f"{node['name']} completed in {dur:.1f} ms"
        else:
            return
        self.logs.setdefault((svc, pod), []).append({
            "timeUnixNano": str(int(t)), "severityNumber": sev[0], "severityText": sev[1],
            "body": {"stringValue": body}, "traceId": trace, "spanId": span, "attributes": _attrs(attrs)})


def _attrs(d):
    return [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)}}
            for k, v in d.items()]


def _resource(svc, pod):
    lang, version, _ = SERVICES[svc]
    return {"attributes": _attrs({
        "service.name": svc, "service.version": version, "service.namespace": "shop",
        "deployment.environment": "production", "k8s.namespace.name": "shop", "k8s.pod.name": pod,
        "host.name": NODES[int(hashlib.md5(pod.encode()).hexdigest(), 16) % len(NODES)],
        "telemetry.sdk.language": lang, "telemetry.sdk.name": "opentelemetry"})}


# ------------------------------------------------------------------ metrics

def _orders_rate(minute, method):
    """Orders placed per minute (deterministic): checkout traffic x success rate x method share."""
    r = random.Random(f"orders/{minute}/{method}")
    ok = 0.7 if incident(minute) else 0.97
    return round(PEAK_REQUESTS_PER_MINUTE * load(minute) * 0.1 * ok * (0.75 if method == "card" else 0.25) * r.uniform(0.8, 1.2))


def _orders_total(minute, pod_i, method):
    """A pod's cumulative orders counter: counts since its last restart (every 6 hours, staggered)."""
    since = minute - (minute + 97 * pod_i) % 360
    return sum(_orders_rate(m, method) for m in range(since, minute + 1)) // 2, since


def metrics(m: Minute):
    minute, r = m.minute, random.Random(f"metrics/{m.minute}")
    t = (minute * 60 + 59) * 10**9
    now, start = str(t), str(t - 60 * 10**9)
    out = {}
    for svc in SERVICES:
        for pod_i, pod in enumerate(PODS[svc]):
            ms = []
            routes = {k[2]: v for k, v in m.requests.items() if k[0] == svc and k[1] == pod}
            if routes:
                ms.append({"name": "http.server.request.duration", "unit": "ms",
                           "description": "Duration of inbound requests", "histogram": {
                               "aggregationTemporality": 1, "dataPoints": [{
                                   "startTimeUnixNano": start, "timeUnixNano": now, "attributes": _attrs({"http.route": route}),
                                   "count": str(len(d)), "sum": sum(d), "min": min(d), "max": max(d), "explicitBounds": BOUNDS,
                                   "bucketCounts": [str(sum(1 for x in d if lo < x <= hi))
                                                    for lo, hi in zip([-1] + BOUNDS, BOUNDS + [float("inf")])]}
                                   for route, d in sorted(routes.items())]}})
            busy = load(minute) * (3 if svc == "shipping" and incident(minute) else 1)
            ms.append({"name": "process.cpu.utilization", "unit": "1", "description": "CPU in use (0-1)", "gauge": {
                "dataPoints": [{"timeUnixNano": now, "asDouble": round(min(0.97, 0.08 + 0.3 * busy + r.gauss(0, 0.03)), 4)}]}})
            base_mb = {"java": 520, "dotnet": 310, "nodejs": 180, "python": 150}.get(SERVICES[svc][0], 60)
            gc = (minute % 17) / 17                                     # sawtooth: grows, then collected
            ms.append({"name": "process.memory.usage", "unit": "By", "description": "Memory in use", "sum": {
                "aggregationTemporality": 2, "isMonotonic": False, "dataPoints": [{
                    "startTimeUnixNano": start, "timeUnixNano": now,
                    "asInt": str(int((base_mb * (1 + 0.35 * gc) + r.gauss(0, 4)) * 2**20))}]}})
            if svc in ("cart", "product-catalog"):
                used = max(1, round(18 * load(minute) + r.gauss(0, 2)))
                ms.append({"name": "db.client.connections.usage", "unit": "{connection}",
                           "description": "Connections in the pool", "sum": {
                               "aggregationTemporality": 2, "isMonotonic": False, "dataPoints": [
                                   {"startTimeUnixNano": start, "timeUnixNano": now, "asInt": str(n),
                                    "attributes": _attrs({"state": s, "pool.name": f"{svc}-db"})}
                                   for s, n in (("used", used), ("idle", max(0, 25 - used)))]}})
            if svc in ("accounting", "fraud-detection"):
                into = minute % INCIDENT_EVERY   # the queue backs up during the incident, then drains
                backlog = 330 * into if into < INCIDENT_FOR else 330 * INCIDENT_FOR * math.exp(-(into - INCIDENT_FOR) / 8)
                lag = r.randint(0, 40) + int(backlog)
                ms.append({"name": "kafka.consumer.lag", "unit": "{message}", "description": "Messages behind",
                           "gauge": {"dataPoints": [{"timeUnixNano": now, "asInt": str(lag),
                                                     "attributes": _attrs({"messaging.destination.name": "orders"})}]}})
            if svc == "checkout":
                pts = []
                for method in ("card", "paypal"):
                    total, since = _orders_total(minute, pod_i, method)
                    pts.append({"startTimeUnixNano": str(since * 60 * 10**9), "timeUnixNano": now, "asInt": str(total),
                                "attributes": _attrs({"app.payment.method": method})})
                ms.append({"name": "app.orders.placed", "unit": "{order}", "description": "Orders placed",
                           "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": pts}})
            if svc == "payment":
                ms.append({"name": "app.payments.declined", "unit": "{payment}", "description": "Payments declined",
                           "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [{
                               "startTimeUnixNano": start, "timeUnixNano": now, "asInt": str(m.declines.get(pod, 0))}]}})
            out[(svc, pod)] = ms
    return out


def documents(minute):
    """-> {"traces": doc, "logs": doc, "metrics": doc}: one OTLP JSON request per signal."""
    m = Minute(minute)
    mets = metrics(m)
    return {
        "traces": {"resourceSpans": [{"resource": _resource(s, p), "scopeSpans": [{"scope": {"name": s}, "spans": v}]}
                                     for (s, p), v in sorted(m.spans.items())]},
        "logs": {"resourceLogs": [{"resource": _resource(s, p), "scopeLogs": [{"scope": {"name": s}, "logRecords": v}]}
                                  for (s, p), v in sorted(m.logs.items())]},
        "metrics": {"resourceMetrics": [{"resource": _resource(s, p), "scopeMetrics": [{"scope": {"name": s}, "metrics": v}]}
                                        for (s, p), v in sorted(mets.items())]},
    }


# ------------------------------------------------------------------ sending

_key = None


def _api_key():
    global _key
    if _key is None:
        _key = boto3.client("ssm").get_parameter(Name=KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _key


def send(signal, doc, patient=False):
    """POST one request; retries throttling and server errors. patient (backfills): also waits out
    a 403, which a brand-new key gets on some requests for up to ~10 minutes."""
    body = gzip.compress(json.dumps(doc, separators=(",", ":")).encode())
    tries = 30 if patient else 5
    for attempt in range(tries):
        req = urllib.request.Request(f"{ENDPOINT}/v1/{signal}", data=body, method="POST", headers={
            "x-api-key": _api_key(), "Content-Type": "application/json", "Content-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            retry = e.code in (429, 500, 502, 503, 504) or (patient and e.code == 403)
            if not retry or attempt == tries - 1:
                return e.code
            if e.code == 403:
                time.sleep(25)
                continue
        except OSError:
            if attempt == tries - 1:
                return 0
        time.sleep(min(0.5 * 2 ** attempt, 10))


def run(minutes, patient=False):
    sent = {}
    for minute in minutes:
        for signal, doc in documents(minute).items():
            status = send(signal, doc, patient)
            sent[status] = sent.get(status, 0) + 1
    return sent


def handler(event, context):
    event = event or {}
    if "backfill_hours" in event:
        first = int(time.time() // 60) - 60 * int(event["backfill_hours"])
        lam = boto3.client("lambda")
        for h in range(int(event["backfill_hours"])):
            lam.invoke(FunctionName=SELF, InvocationType="Event",
                       Payload=json.dumps({"from_minute": first + 60 * h, "minutes": 60}).encode())
        out = {"backfill_invocations": int(event["backfill_hours"])}
    elif "from_minute" in event:
        start = int(event["from_minute"])
        out = {"from_minute": start, "sent": run(range(start, start + int(event.get("minutes", 1))), patient=True)}
    else:
        out = {"minute": int(time.time() // 60) - 1, "sent": run([int(time.time() // 60) - 1])}
    print(json.dumps(out))
    return out
