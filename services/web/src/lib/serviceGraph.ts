// The service map from the eBPF agent's metrics (docs/ebpf-design.md): one aggregate query over its
// service-graph counters and histograms, turned into nodes and edges and merged with the ones the
// SDK traces give. Pure functions, tested in serviceGraph.test.ts.
import type { Query } from "../api";

export const TOTAL = "traces_service_graph_request_total";
export const FAILED = "traces_service_graph_request_failed_total";
export const SERVER = "traces_service_graph_request_server";   // histogram, seconds, server side
export const CLIENT = "traces_service_graph_request_client";   // ... and the caller's side (a database's only one)
export const EBPF_DISTRO = "opentelemetry-ebpf-instrumentation";      // resource telemetry.distro.name
export const OUTSIDE = "outside";

type W = { start: string; end: string };
export type GraphNode = { service: string; requests: number; errors: number; avg_ms: number; kind: string; ebpf: boolean };
export type GraphEdge = { source: string; target: string; calls: number; errors: number };

/** Per metric and reporting service, the rise of each connection's counters and its server-side time. */
export const graphQuery = (w: W): Query => ({
  signal: "metrics", ...w,
  where: [{ field: "metric_name", op: "in", value: [TOTAL, FAILED, SERVER, CLIENT] }],
  group_by: ["metric_name", "service", "attributes.client", "attributes.server", "attributes.connection_type"],
  aggs: [{ fn: "increase", field: "value" }, { fn: "increase", field: "count" }, { fn: "increase", field: "sum" }],
  limit: 10_000,
});

const IP = /^(\d{1,3}\.){3}\d{1,3}$|^[0-9a-f:]*:[0-9a-f:]+$/i;
/** A peer the agent could only name by its address is outside what Leasyd sees: one node for all of them. */
export const peerName = (s: unknown) => { const v = String(s ?? ""); return !v || IP.test(v) ? OUTSIDE : v; };

const num = (v: unknown) => Number(v) || 0;

/** Rows of graphQuery -> nodes and edges. A connection's calls are the server's count when the
 *  server reported it (instrumented), else the client's (a database, a cache, an outside API): the
 *  agent counts each call once. Its failures are the larger side's: both may count the same one, and
 *  each side can see some the other doesn't (an HTTP 404 fails the caller, not the server). */
export function ebpfGraph(rows: Record<string, unknown>[]): { nodes: GraphNode[]; edges: GraphEdge[] } {
  type Acc = { total: number; failed: number; serverSide: boolean; count: number; sum: number; type: string };
  const conn = new Map<string, { server?: Acc; client?: Acc }>();
  for (const r of rows) {
    const client = peerName(r["attributes.client"]), server = peerName(r["attributes.server"]);
    if (client === server) continue;
    const side = String(r.service) === String(r["attributes.server"]) ? "server" : "client";
    const k = client + "\u0000" + server;
    const c = conn.get(k) ?? {};
    const a = (c[side] ??= { total: 0, failed: 0, serverSide: side === "server", count: 0, sum: 0, type: "" });
    const m = String(r.metric_name);
    if (m === TOTAL) a.total += num(r["increase(value)"]);
    else if (m === FAILED) a.failed += num(r["increase(value)"]);
    else if (m === SERVER || m === CLIENT) { a.count += num(r["increase(count)"]); a.sum += num(r["increase(sum)"]); }
    a.type ||= String(r["attributes.connection_type"] ?? "");
    conn.set(k, c);
  }
  const edges: GraphEdge[] = [];
  const nodes = new Map<string, GraphNode>();
  const node = (s: string, kind = "service") => {
    let n = nodes.get(s);
    if (!n) nodes.set(s, (n = { service: s, requests: 0, errors: 0, avg_ms: 0, kind: s === OUTSIDE ? "outside" : kind, ebpf: true }));
    return n;
  };
  const time = new Map<string, { count: number; sum: number }>();
  for (const [k, c] of conn) {
    const [source, target] = k.split("\u0000");
    const a = c.server ?? c.client!;
    const calls = a.total || c.client?.total || 0, errors = Math.max(c.server?.failed ?? 0, c.client?.failed ?? 0);
    if (!calls && !errors) continue;
    edges.push({ source, target, calls, errors });
    node(source);
    const t = node(target, c.server ? "service" : kindOf(a.type));
    t.requests += calls; t.errors += errors;
    // time as the server measured it, else as its callers did (a database, an outside API)
    const timed = c.server?.count ? c.server : c.client;
    if (timed?.count) { const s = time.get(target) ?? { count: 0, sum: 0 }; s.count += timed.count; s.sum += timed.sum; time.set(target, s); }
  }
  for (const [s, t] of time) if (t.count) node(s).avg_ms = (t.sum / t.count) * 1000;
  return { nodes: [...nodes.values()], edges };
}

const kindOf = (type: string) => (type === "database" ? "database" : type === "messaging_system" ? "queue" : "external");

/** SDK (trace) and eBPF views of the same map as one: a service or connection both see keeps the larger
 *  count of each (never their sum: the agent leaves a process exporting its own OTLP alone, and the
 *  service-graph counters are not spans). */
export function mergeGraphs<N extends { service: string; requests: number; errors: number; avg_ms: number }>(
  sdk: { nodes: N[]; edges: GraphEdge[] }, ebpf: { nodes: GraphNode[]; edges: GraphEdge[] },
  blank: (s: string) => N,
): { nodes: (N & { kind: string; ebpf: boolean; sdk: boolean })[]; edges: GraphEdge[] } {
  const nodes = new Map(sdk.nodes.map((n) => [n.service, { ...n, kind: "service", ebpf: false, sdk: true }]));
  for (const e of ebpf.nodes) {
    const n = nodes.get(e.service);
    if (!n) { nodes.set(e.service, { ...blank(e.service), requests: e.requests, errors: e.errors, avg_ms: e.avg_ms, kind: e.kind, ebpf: true, sdk: false }); continue; }
    if (e.requests > n.requests) Object.assign(n, { requests: e.requests, errors: e.errors, avg_ms: e.avg_ms || n.avg_ms });
    n.ebpf = n.ebpf || e.requests > 0;
  }
  const edges = new Map(sdk.edges.map((e) => [e.source + ">" + e.target, { ...e }]));
  for (const e of ebpf.edges) {
    const k = e.source + ">" + e.target, have = edges.get(k);
    if (!have) edges.set(k, { ...e });
    else { have.calls = Math.max(have.calls, e.calls); have.errors = Math.max(have.errors, e.errors); }
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

// ------------------------------------------------------------ one service's panel, from the same metrics

/** Over time, one service's (or database's) side of the service graph: the server's own series when
 *  it reported them (an instrumented service), else its callers' (a database, an outside API). */
export const serviceSeriesQuery = (w: W, service: string, bucketS: number): Query => ({
  signal: "metrics", ...w,
  where: [{ field: "metric_name", op: "in", value: [TOTAL, FAILED, SERVER, CLIENT] }, { field: "attributes.server", op: "=", value: service }],
  group_by: [`ts:${bucketS}`, "metric_name", "service"],
  aggs: [{ fn: "increase", field: "value" }, { fn: "increase", field: "count" }, { fn: "increase", field: "sum" }],
  limit: 10_000,
});

export type Point = { t: string; n: number; e: number; ms: number };
export function serviceSeries(rows: Record<string, unknown>[], service: string): Point[] {
  const own = rows.some((r) => String(r.service) === service);
  const at = new Map<string, { n: number; e: number; count: number; sum: number }>();
  for (const r of rows) {
    if (own && String(r.service) !== service) continue;
    const t = String(r.ts ?? r[Object.keys(r).find((k) => k.startsWith("ts")) ?? "ts"]);
    const p = at.get(t) ?? { n: 0, e: 0, count: 0, sum: 0 };
    const m = String(r.metric_name);
    if (m === TOTAL) p.n += num(r["increase(value)"]);
    else if (m === FAILED) p.e += num(r["increase(value)"]);
    else if (m === SERVER || m === CLIENT) { p.count += num(r["increase(count)"]); p.sum += num(r["increase(sum)"]); }
    at.set(t, p);
  }
  return [...at].sort(([a], [b]) => a.localeCompare(b))
    .map(([t, p]) => ({ t, n: p.n, e: p.e, ms: p.count ? (p.sum / p.count) * 1000 : 0 }));
}

const SERVER_DURATIONS = ["http.server.request.duration", "rpc.server.duration"];
/** One service's operations (route or RPC method) as the agent measured them: calls, failures, time. */
export const operationsQuery = (w: W, service: string): Query => ({
  signal: "metrics", ...w, services: [service],
  where: [{ field: "metric_name", op: "in", value: SERVER_DURATIONS }],
  group_by: ["attributes.http.request.method", "attributes.http.route", "attributes.rpc.method", "attributes.error.type"],
  aggs: [{ fn: "increase", field: "count" }, { fn: "increase", field: "sum" }],
  limit: 2000,
});

export type Operation = { name: string; n: number; e: number; avg_ms: number };
export function operations(rows: Record<string, unknown>[]): Operation[] {
  const ops = new Map<string, { n: number; e: number; sum: number }>();
  for (const r of rows) {
    const method = r["attributes.http.request.method"], route = r["attributes.http.route"], rpc = r["attributes.rpc.method"];
    const name = rpc ? String(rpc) : [method, route].filter(Boolean).join(" ") || "(other)";
    const o = ops.get(name) ?? { n: 0, e: 0, sum: 0 };
    const n = num(r["increase(count)"]);
    o.n += n; o.sum += num(r["increase(sum)"]);
    if (r["attributes.error.type"]) o.e += n;
    ops.set(name, o);
  }
  return [...ops].map(([name, o]) => ({ name, n: o.n, e: o.e, avg_ms: o.n ? (o.sum / o.n) * 1000 : 0 }))
    .sort((a, b) => b.n - a.n);
}

/** Where an eBPF-seen service runs (the panel's summary), from its metrics' resource attributes. */
export const WHERE_FIELDS: [string, string][] = [
  ["k8s_namespace", "resource.k8s.namespace.name"], ["k8s_deployment", "resource.k8s.deployment.name"],
  ["k8s_pod", "resource.k8s.pod.name"], ["host", "resource.host.name"], ["language", "resource.telemetry.sdk.language"],
];
export const whereQuery = (w: W, service: string): Query => ({
  signal: "metrics", ...w, services: [service],
  where: [{ field: "metric_name", op: "in", value: [SERVER, ...SERVER_DURATIONS] }],
  group_by: WHERE_FIELDS.map(([, f]) => f), aggs: [{ fn: "count" }], limit: 1,
});
