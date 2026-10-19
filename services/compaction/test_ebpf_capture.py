"""The zero-code APM over what the eBPF agent really sent: 12 minutes captured from the eBPF lab
(infra/ebpf-lab.sh: k3s on Ubuntu 24.04, kernel 7.0, examples/kubernetes/leasyd-k8s.sh with the
eBPF agent on, infra/ebpf-lab/workloads.yaml: a shop with no OpenTelemetry in it, Python, Node.js,
PostgreSQL and Redis) in testdata/ebpf-capture.json.gz, after the node's collector sampled the traces.
Checks the service map's query (web lib/serviceGraph.ts graphQuery) and what the traces carry."""
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE = os.path.join(HERE, "testdata", "ebpf-capture.json.gz")
GRAPH = ["traces_service_graph_request_total", "traces_service_graph_request_failed_total", "traces_service_graph_request_server"]


def _times(docs):
    for doc in docs:
        for r in doc.get("resourceMetrics", []):
            for s in r.get("scopeMetrics", []):
                for m in s.get("metrics", []):
                    for kind in ("gauge", "sum", "histogram"):
                        for p in (m.get(kind) or {}).get("dataPoints", []):
                            yield int(p["timeUnixNano"])
        for r in doc.get("resourceSpans", []):
            for s in r.get("scopeSpans", []):
                for sp in s.get("spans", []):
                    yield int(sp["startTimeUnixNano"])


@pytest.fixture(scope="module")
def lab():
    from localengine import LocalEngine
    data = json.load(gzip.open(CAPTURE, "rt"))
    e, lo, hi = LocalEngine(), None, None
    for f in data["files"]:
        ts = list(_times(f["docs"]))
        first, last = (datetime.fromtimestamp(t / 1e9, timezone.utc) for t in (min(ts), max(ts)))
        e.load(f["signal"], f["docs"], first)
        lo, hi = min(lo or first, first), max(hi or last, last)
    e.window = {"start": (lo - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": (hi + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    yield e
    e.close()


def rows(out):
    return [dict(zip(out["columns"], r)) for r in out["rows"]]


def test_the_service_map_query_finds_every_connection(lab):
    out = lab.query_json({**lab.window, "signal": "metrics", "where": [{"field": "metric_name", "op": "in", "value": GRAPH}],
                          "group_by": ["metric_name", "service", "attributes.client", "attributes.server", "attributes.connection_type"],
                          "aggs": [{"fn": "increase", "field": "value"}, {"fn": "increase", "field": "count"}, {"fn": "increase", "field": "sum"}],
                          "limit": 10000})
    g = rows(out)
    calls = {(r["attributes.client"], r["attributes.server"]): r["increase(value)"] for r in g
             if r["metric_name"] == GRAPH[0] and r["service"] == r["attributes.server"]}
    assert set(calls) == {("loadgen", "frontend"), ("frontend", "cart"), ("frontend", "catalog"),
                          ("cart", "catalog"), ("cart", "redis"), ("catalog", "postgres")}
    assert all(v > 50 for v in calls.values()), calls
    # Redis and PostgreSQL are seen as servers too: every Redis command and SQL query of the shop
    assert calls[("cart", "redis")] > calls[("frontend", "cart")]
    failed = {(r["attributes.client"], r["attributes.server"], r["service"]): r["increase(value)"] for r in g if r["metric_name"] == GRAPH[1]}
    assert failed.get(("loadgen", "frontend", "frontend"), 0) > 0          # the frontend's 502s
    assert failed.get(("frontend", "cart", "frontend"), 0) > 0            # cart's 404s, failing its caller
    time = {r["attributes.server"]: (r["increase(sum)"], r["increase(count)"]) for r in g
            if r["metric_name"] == GRAPH[2] and r["attributes.client"] == "loadgen"}
    s, n = time["frontend"]
    assert 0.003 < s / n < 1.2                                            # seconds a checkout


def test_routes_are_grouped_and_failures_marked(lab):
    out = lab.query_json({**lab.window, "signal": "metrics", "where": [{"field": "metric_name", "op": "=", "value": "http.server.request.duration"}],
                          "group_by": ["service", "attributes.http.request.method", "attributes.http.route", "attributes.error.type"],
                          "aggs": [{"fn": "increase", "field": "count"}], "limit": 1000})
    seen = {(r["service"], r["attributes.http.route"], r["attributes.error.type"]) for r in rows(out) if r["increase(count)"]}
    assert {("catalog", "/products/*", None), ("cart", "/cart/*", None), ("frontend", "/checkout", None),
            ("frontend", "/checkout", "502")} <= seen


def test_traces_carry_the_workload_and_keep_every_error(lab):
    out = lab.query_json({**lab.window, "signal": "traces", "where": [{"field": "kind", "op": "=", "value": 2}],
                          "group_by": ["service", "resource.k8s.namespace.name", "resource.k8s.deployment.name",
                                       "resource.telemetry.distro.name", "resource.k8s.cluster.name"],
                          "aggs": [{"fn": "count"}]})
    got = {(r["service"], r["resource.k8s.namespace.name"], r["resource.k8s.deployment.name"],
            r["resource.telemetry.distro.name"], r["resource.k8s.cluster.name"]) for r in rows(out)}
    assert ("frontend", "shop", "frontend", "opentelemetry-ebpf-instrumentation", "ebpf-lab") in got
    assert {g[0] for g in got} >= {"frontend", "cart", "catalog", "redis", "postgres"}
    # sampled on the node: every failed checkout kept, about a tenth of the others plus the slow ones
    spans = rows(lab.query_json({**lab.window, "signal": "traces", "services": ["frontend"],
                                 "where": [{"field": "kind", "op": "=", "value": 2}], "group_by": ["status_code"], "aggs": [{"fn": "count"}]}))
    kept = {r["status_code"]: r["count"] for r in spans}
    counted = rows(lab.query_json({**lab.window, "signal": "metrics", "services": ["frontend"],
                                   "where": [{"field": "metric_name", "op": "=", "value": "http.server.request.duration"}],
                                   "group_by": ["attributes.error.type"], "aggs": [{"fn": "increase", "field": "count"}]}))
    ok = sum(r["increase(count)"] or 0 for r in counted if not r["attributes.error.type"])
    failed = sum(r["increase(count)"] or 0 for r in counted if r["attributes.error.type"])
    assert kept[2] >= failed * 0.9                       # (the metrics skip each series' first minute)
    assert 0.05 * ok < kept[0] < 0.4 * ok
