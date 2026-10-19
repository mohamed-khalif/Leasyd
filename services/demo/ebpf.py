"""Demo eBPF data: what OpenTelemetry eBPF Instrumentation (docs/ebpf-design.md) would add to the
shop, in the shape the eBPF lab captured (services/compaction/testdata/ebpf-capture.json.gz): parts
of the shop no SDK instruments, seen from the kernel.

  checkout (SDK) -> inventory (legacy PHP, eBPF only) -> orders-db (Amazon RDS, a database: seen from
                    its callers) and redis (eBPF) and pricing (eBPF only) -> partner API (outside)

Per minute: the service-graph counters (cumulative, restarting every 6 hours like the pods) and the
server-side duration histograms, plus the HTTP request-duration metrics with routes. Seeded by the
minute, so every minute is the same each time it is made. The incident (demo.incident) slows the
partner API and fails some pricing calls.
"""
import functools
import hashlib
import random

import demo

SCOPE = "go.opentelemetry.io/obi"
DISTRO = "opentelemetry-ebpf-instrumentation"
SERIES_RESET = 360          # minutes: counters restart with the pods
BOUNDS = [0.0, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0]   # OBI's default (s)

# client, server, reported by, connection_type, calls per checkout, failure ratio, seconds
EDGES = [
    ("checkout", "inventory", "inventory", "", 1.0, 0.01, 0.040),
    ("inventory", "redis", "redis", "", 3.0, 0.0, 0.0008),
    ("inventory", "orders-db.c9xk2pq1.us-east-1.rds.amazonaws.com", "inventory", "database", 2.0, 0.002, 0.006),
    ("inventory", "pricing", "pricing", "", 1.0, 0.004, 0.012),
    ("pricing", "api.partner-rates.com", "pricing", "virtual_node", 0.5, 0.01, 0.180),
]
SERVICES = {"inventory": "php", "redis": "unknown", "pricing": "rust"}
ROUTES = {"inventory": [("GET", "/stock/*"), ("POST", "/reserve")], "pricing": [("GET", "/quote/*")]}


@functools.lru_cache(maxsize=20_000)
def _calls(minute, edge):
    """Calls on one connection in one minute, and how many failed."""
    client, server, _, _, per, fail, _ = edge
    r = random.Random(f"ebpf/{minute}/{client}>{server}")
    checkouts = demo.PEAK_REQUESTS_PER_MINUTE * demo.load(minute) * 0.1   # as the shop's checkout traffic
    n = max(0, round(checkouts * per * r.uniform(0.85, 1.15)))
    if demo.incident(minute) and server in ("pricing", "api.partner-rates.com"):
        fail = 0.08
    return n, min(n, round(n * fail * r.uniform(0.5, 1.5) + (r.random() < n * fail % 1)))


@functools.lru_cache(maxsize=20_000)
def _since_reset(minute, edge, share=1.0):
    """(calls, failed, seconds, buckets, seconds of the failed) on one connection from its series'
    start to minute: every one only ever grows, as a cumulative counter must."""
    n, f = _calls(minute, edge)
    n, f = round(n * share), round(f * share)
    s = _secs(minute, edge)
    here = (n, f, n * s, tuple(_hist(n, s)), f * s)
    if minute % SERIES_RESET == 0:
        return here
    a = _since_reset(minute - 1, edge, share)
    return (a[0] + here[0], a[1] + here[1], a[2] + here[2], tuple(x + y for x, y in zip(a[3], here[3])), a[4] + here[4])


def _secs(minute, edge):
    s = edge[6]
    return s * 6 if demo.incident(minute) and edge[1] == "api.partner-rates.com" else s


def _hist(count, mean):
    """count calls around mean seconds over BOUNDS: most in its bucket, some slower."""
    b = [0] * (len(BOUNDS) + 1)
    i = next((k for k, x in enumerate(BOUNDS) if mean <= x), len(BOUNDS))
    main, slow = round(count * 0.8), round(count * 0.15)
    b[i] += main
    b[min(i + 1, len(BOUNDS))] += slow
    b[min(i + 2, len(BOUNDS))] += count - main - slow
    return b


def _kv(d):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in d.items() if v not in (None, "")]


def _resource(service):
    pod = f"{service}-{hashlib.md5(service.encode()).hexdigest()[:9]}-{hashlib.md5(service[::-1].encode()).hexdigest()[:5]}"
    return {"attributes": _kv({
        "service.name": service, "service.namespace": "shop", "k8s.namespace.name": "shop", "k8s.deployment.name": service,
        "k8s.pod.name": pod, "k8s.cluster.name": "shop-prod", "telemetry.sdk.name": "opentelemetry",
        "telemetry.sdk.language": SERVICES.get(service, "unknown"), "telemetry.distro.name": DISTRO})}


def resource_metrics(minute):
    """-> resourceMetrics of the eBPF agent for one minute (one resource per reporting service)."""
    start = minute - minute % SERIES_RESET
    t, t0 = str(minute * 60 * 10**9 + 59 * 10**9), str(start * 60 * 10**9)
    by = {}
    for edge in EDGES:
        client, server, reporter, ctype = edge[:4]
        total, failed, secs, buckets, _ = _since_reset(minute, edge)    # cumulative since the series started
        count = total
        attrs = _kv({"client": client, "server": server, "connection_type": ctype, "client_service_namespace": "shop",
                     "server_service_namespace": "shop" if "." not in server else "", "source": "obi"})
        point = {"attributes": attrs, "startTimeUnixNano": t0, "timeUnixNano": t}
        side = "server" if reporter == server else "client"
        mets = by.setdefault(reporter, [])
        mets.append({"name": "traces_service_graph_request_total",
                     "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [{**point, "asInt": str(total)}]}})
        if failed:
            mets.append({"name": "traces_service_graph_request_failed_total",
                         "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [{**point, "asInt": str(failed)}]}})
        mets.append({"name": f"traces_service_graph_request_{side}", "unit": "s",
                     "histogram": {"aggregationTemporality": 2, "dataPoints": [{**point, "count": str(count), "sum": secs,
                                   "bucketCounts": [str(x) for x in buckets], "explicitBounds": BOUNDS}]}})
    for service, routes in ROUTES.items():       # per route, as http.server.request.duration
        incoming = [e for e in EDGES if e[1] == service]
        for i, (method, route) in enumerate(routes):
            share = (0.7, 0.3)[i] if len(routes) > 1 else 1.0
            sums = [_since_reset(minute, e, share) for e in incoming]
            count, failed, secs, fsecs = (sum(x[k] for x in sums) for k in (0, 1, 2, 4))
            pts = [{"attributes": _kv({"http.request.method": method, "http.route": route, "http.response.status_code": "200",
                                       "url.scheme": "http"}), "count": str(count - failed), "sum": secs - fsecs},
                   {"attributes": _kv({"http.request.method": method, "http.route": route, "http.response.status_code": "500",
                                       "error.type": "500", "url.scheme": "http"}), "count": str(failed), "sum": fsecs}]
            for p in pts:
                p.update(startTimeUnixNano=t0, timeUnixNano=t, bucketCounts=[p["count"]] + ["0"] * len(BOUNDS), explicitBounds=BOUNDS)
            by.setdefault(service, []).append({"name": "http.server.request.duration", "unit": "s",
                                               "histogram": {"aggregationTemporality": 2, "dataPoints": [p for p in pts if p["count"] != "0"]}})
    return [{"resource": _resource(s), "scopeMetrics": [{"scope": {"name": SCOPE}, "metrics": mets}]} for s, mets in sorted(by.items())]
