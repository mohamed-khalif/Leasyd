// Services as the APM page and the service map see them: from the per-minute span summaries
// (svc_1m / edge_1m) merged with the eBPF agent's service-graph metrics (lib/serviceGraph.ts), and
// one service's numbers over time and its operations (from its spans, or the agent's metrics for a
// service only the agent sees: its spans are sampled, its metrics exact).
import { useMemo } from "react";
import { fmtNum } from "./format";
import { sqlString as q } from "./sql";
import { records, Result } from "../api";
import type { Ctx } from "../App";
import { type Loaded, useQuery, useSql } from "../useQuery";
import { ebpfGraph, mergeGraphs, graphQuery, operations, operationsQuery, serviceSeries, serviceSeriesQuery, WHERE_FIELDS, whereQuery } from "./serviceGraph";
import { bucketSeconds } from "../time";
import { useRangeWindow } from "../useNow";

export type Svc = { service: string; requests: number; errors: number; avg_ms: number; attrs: Record<string, string>; kind: string; ebpf: boolean; sdk: boolean };
export type Edge = { source: string; target: string; calls: number; errors: number };

// A span that starts work in its service: a server or consumer span, or a root.
export const ENTRY = "(kind IN (2, 5) OR parent_span_id IS NULL)";
export const ATTRS: [string, string][] = [
  ["namespace", "service.namespace"], ["version", "service.version"], ["language", "telemetry.sdk.language"],
  ["runtime", "process.runtime.name"], ["runtime_version", "process.runtime.version"],
  ["k8s_namespace", "k8s.namespace.name"], ["k8s_deployment", "k8s.deployment.name"], ["k8s_pod", "k8s.pod.name"],
  ["cloud", "cloud.provider"], ["platform", "cloud.platform"], ["account", "cloud.account.id"], ["region", "cloud.region"], ["zone", "cloud.availability_zone"],
  ["host", "host.name"],
];
// From the per-minute summaries (compact.SUMMARIES svc_1m / edge_1m), not every span.
const NODES_SQL = `SELECT service, sum(requests) AS requests, sum(errors) AS errors,
  sum(duration_ns_sum) / nullif(sum(requests), 0) / 1e6 AS avg_ms,
  ${ATTRS.map(([k]) => `any_value(${k}) AS ${k}`).join(", ")}
FROM svc_1m GROUP BY service`;
const EDGES_SQL = `SELECT source, target, sum(calls) AS calls, sum(errors) AS errors FROM edge_1m GROUP BY 1, 2`;

export const errPct = (n: { requests: number; errors: number }) => (n.requests ? (n.errors / n.requests) * 100 : 0);
export const perMin = (n: number, minutes: number) => `${fmtNum(n / Math.max(minutes, 1))}/m`;
export const health = (pct: number) => (pct >= 5 ? "critical" : pct >= 1 ? "warning" : "ok");

// telemetry.sdk.language -> its runtime dashboard
export const RUNTIME_DASHBOARDS: Record<string, [string, string]> = { java: ["builtin-jvm", "JVM dashboard"], nodejs: ["builtin-nodejs", "Node.js dashboard"],
                                                                     python: ["builtin-python", "Python dashboard"], ruby: ["builtin-ruby", "Ruby dashboard"] };

/** Two results as one for <Loads>: the first error, data once both have come (cut if either was). */
export function both(a: Loaded, b: Loaded): Loaded {
  return { error: a.error || b.error, loading: a.loading || b.loading,
           data: a.data && b.data ? { ...b.data, truncated: a.data.truncated || b.data.truncated } : null };
}

/** Every service and call in the page's range. The eBPF view adds what the traces don't show; its
 *  query failing leaves the trace view as it was. */
export function useServices(ctx: Ctx) {
  const w = useRangeWindow(ctx);
  const key = ctx.range.key + ctx.tick;
  const nq = useSql(NODES_SQL, w, "n" + key), eq = useSql(EDGES_SQL, w, "e" + key), gq = useQuery(graphQuery(w), "g" + key);
  const graph = useMemo(() => {
    if (!nq.data || !eq.data) return { nodes: [] as Svc[], edges: [] as Edge[] };
    const sdkEdges = records(eq.data).map((r) => ({ source: String(r.source), target: String(r.target), calls: Number(r.calls) || 0, errors: Number(r.errors) || 0 }));
    const sdkNodes = records(nq.data).map((r) => ({
      service: String(r.service), requests: Number(r.requests) || 0, errors: Number(r.errors) || 0, avg_ms: Number(r.avg_ms) || 0,
      attrs: Object.fromEntries(ATTRS.map(([k]) => [k, r[k] == null ? "" : String(r[k])])),
    }));
    const merged = mergeGraphs({ nodes: sdkNodes, edges: sdkEdges }, ebpfGraph(gq.data ? records(gq.data) : []),
      (service) => ({ service, requests: 0, errors: 0, avg_ms: 0, attrs: Object.fromEntries(ATTRS.map(([k]) => [k, ""])) }));
    return { nodes: merged.nodes as Svc[], edges: merged.edges };
  }, [nq.data, eq.data, gq.data]);
  // the eBPF query failing leaves the trace view: counted as answered (with nothing) rather than pending
  return { ...graph, loaded: both(both(nq, eq), gq.error ? { data: nq.data, error: null, loading: false } : gq) };
}

export type SeriesPoint = { t: number; n: number; e: number; ms: number; p95: number | null };
export type OpRow = { name: string; n: number; e: number; avg_ms: number; p95_ms?: number };

/** One service's numbers over time, its operations (when wanted) and where it runs. */
export function useServiceDetail(ctx: Ctx, node: Svc, wantOps: boolean) {
  const w = useRangeWindow(ctx);
  const b = bucketSeconds(ctx.range);
  const key = node.service + ctx.range.key + ctx.tick;
  const ebpf = node.ebpf && !node.sdk;   // both: the SDK's spans are complete
  const series = useSql(ebpf ? null : `SELECT time_bucket(INTERVAL '${b} seconds', ts) AS t, count(*) AS n,
      count(*) FILTER (WHERE status_code = 2) AS e, avg(duration_ns) / 1e6 AS ms, quantile_cont(duration_ns, 0.95) / 1e6 AS p95
    FROM spans WHERE service = ${q(node.service)} AND ${ENTRY} GROUP BY 1 ORDER BY 1`, w, "s" + key);
  const mseries = useQuery(ebpf ? serviceSeriesQuery(w, node.service, b) : null, "m" + key);
  const ops = useSql(wantOps && !ebpf ? `SELECT name, count(*) AS n, count(*) FILTER (WHERE status_code = 2) AS e,
      avg(duration_ns) / 1e6 AS avg_ms, quantile_cont(duration_ns, 0.95) / 1e6 AS p95_ms
    FROM spans WHERE service = ${q(node.service)} AND ${ENTRY} GROUP BY 1 ORDER BY 2 DESC LIMIT 50` : null, w, "o" + key + wantOps);
  const mops = useQuery(wantOps && ebpf ? operationsQuery(w, node.service) : null, "mo" + key + wantOps);
  const where = useQuery(ebpf && !node.attrs.k8s_namespace && !node.attrs.host ? whereQuery(w, node.service) : null, "w" + key);
  const rows = (r: Result | null) => (r ? records(r) : []);
  const points: SeriesPoint[] = ebpf
    ? serviceSeries(rows(mseries.data), node.service).map((p) => ({ t: Date.parse(p.t), n: p.n, e: p.e, ms: p.ms, p95: null }))
    : rows(series.data).map((r) => ({ t: Date.parse(String(r.t)), n: Number(r.n ?? 0), e: Number(r.e ?? 0), ms: Number(r.ms ?? 0),
                                       p95: r.p95 == null ? null : Number(r.p95) }));
  const opRows: OpRow[] = ebpf ? operations(rows(mops.data))
    : rows(ops.data).map((r) => ({ name: String(r.name), n: Number(r.n), e: Number(r.e), avg_ms: Number(r.avg_ms), p95_ms: Number(r.p95_ms) }));
  const found = rows(where.data)[0] ?? {};
  const attrs: Record<string, string> = { ...node.attrs };
  for (const [k, f] of WHERE_FIELDS) if (!attrs[k] && found[f] != null) attrs[k] = String(found[f]);
  return { ebpf, points, seriesQ: ebpf ? mseries : series, opRows, opsQ: ebpf ? mops : ops, attrs, bucketS: b };
}
