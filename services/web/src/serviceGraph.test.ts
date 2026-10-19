// The service map from the eBPF agent's metrics (lib/serviceGraph.ts), on what the agent really sent
// from the eBPF lab (infra/ebpf-lab.sh: a shop with no OpenTelemetry in it, on k3s), as the query API
// returned it for graphQuery.
import { test } from "node:test";
import assert from "node:assert/strict";
import lab from "./test/ebpf-graph.json";
import { ebpfGraph, mergeGraphs, operations, OUTSIDE, peerName, serviceSeries } from "./lib/serviceGraph";

const rows = lab.rows.map((r) => Object.fromEntries(lab.columns.map((c, i) => [c, r[i]])));

test("the lab's shop as a map: every service and connection, from the agent's metrics alone", () => {
  const { nodes, edges } = ebpfGraph(rows);
  const e = Object.fromEntries(edges.map((x) => [`${x.source}>${x.target}`, [x.calls, x.errors]]));
  assert.deepEqual(e, {
    "loadgen>frontend": [260, 41], "frontend>cart": [258, 41], "frontend>catalog": [219, 0],
    "cart>catalog": [259, 41], "cart>redis": [520, 0], "catalog>postgres": [468, 0],
  });                                       // pip's one-off calls to pypi.org rose by nothing in the range
  const n = Object.fromEntries(nodes.map((x) => [x.service, x]));
  assert.equal(n.catalog.requests, 219 + 259);
  assert.ok(Math.abs(n.frontend.avg_ms - (5.700471471999993 / 260) * 1000) < 1e-9);   // server-side time
  assert.equal(n.frontend.errors, 41);
  assert.ok(nodes.every((x) => x.ebpf));
});

test("peers known only by address are one 'outside' node; databases keep their kind", () => {
  assert.equal(peerName("10.0.3.7"), OUTSIDE);
  assert.equal(peerName("fe80::1"), OUTSIDE);
  assert.equal(peerName("api.stripe.com"), "api.stripe.com");
  const { nodes, edges } = ebpfGraph([
    { metric_name: "traces_service_graph_request_total", service: "shop", "attributes.client": "shop", "attributes.server": "db.internal",
      "attributes.connection_type": "database", "increase(value)": 30 },
    { metric_name: "traces_service_graph_request_client", service: "shop", "attributes.client": "shop", "attributes.server": "db.internal",
      "attributes.connection_type": "database", "increase(count)": 30, "increase(sum)": 0.15 },
    { metric_name: "traces_service_graph_request_total", service: "shop", "attributes.client": "10.0.0.9", "attributes.server": "shop", "increase(value)": 5 },
  ]);
  assert.equal(nodes.find((x) => x.service === "db.internal")!.avg_ms, 5);   // timed by its caller
  assert.deepEqual(edges.map((x) => `${x.source}>${x.target}`).sort(), ["outside>shop", "shop>db.internal"]);
  assert.equal(nodes.find((x) => x.service === "db.internal")!.kind, "database");
  assert.equal(nodes.find((x) => x.service === OUTSIDE)!.kind, "outside");
});

test("traces and eBPF on the same map: the larger count wins, never the sum", () => {
  const blank = (service: string) => ({ service, requests: 0, errors: 0, avg_ms: 0 });
  const m = mergeGraphs(
    { nodes: [{ service: "cart", requests: 300, errors: 2, avg_ms: 5 }, { service: "payments", requests: 10, errors: 0, avg_ms: 50 }],
      edges: [{ source: "cart", target: "payments", calls: 10, errors: 0 }] },
    ebpfGraph(rows), blank);
  const n = Object.fromEntries(m.nodes.map((x) => [x.service, x]));
  assert.equal(n.cart.requests, 300);                // the SDK's spans saw more than the eBPF window
  assert.equal(n.cart.sdk && n.cart.ebpf, true);
  assert.equal(n.payments.ebpf, false);
  assert.equal(n.redis.sdk, false);
  assert.ok(m.edges.some((x) => x.source === "cart" && x.target === "payments"));
  assert.equal(m.edges.find((x) => x.source === "cart" && x.target === "redis")!.calls, 520);
});

test("a service's own series and operations from the agent's metrics", () => {
  const s = serviceSeries([
    { "ts:60": "2026-10-19T23:30:00", metric_name: "traces_service_graph_request_total", service: "cart", "increase(value)": 100 },
    { "ts:60": "2026-10-19T23:30:00", metric_name: "traces_service_graph_request_failed_total", service: "frontend", "increase(value)": 9 },
    { "ts:60": "2026-10-19T23:30:00", metric_name: "traces_service_graph_request_failed_total", service: "cart", "increase(value)": 1 },
    { "ts:60": "2026-10-19T23:30:00", metric_name: "traces_service_graph_request_server", service: "cart", "increase(count)": 100, "increase(sum)": 0.5 },
  ], "cart");
  assert.deepEqual(s, [{ t: "2026-10-19T23:30:00", n: 100, e: 1, ms: 5 }]);   // cart's own side only
  const ops = operations([
    { "attributes.http.request.method": "GET", "attributes.http.route": "/cart/*", "increase(count)": 90, "increase(sum)": 0.9 },
    { "attributes.http.request.method": "GET", "attributes.http.route": "/cart/*", "attributes.error.type": "500", "increase(count)": 10, "increase(sum)": 0.1 },
    { "attributes.rpc.method": "Checkout", "increase(count)": 5, "increase(sum)": 0.05 },
  ]);
  assert.deepEqual(ops, [{ name: "GET /cart/*", n: 100, e: 10, avg_ms: 10 }, { name: "Checkout", n: 5, e: 0, avg_ms: 10 }]);
});
