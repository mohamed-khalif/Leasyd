// Service map: every service as a node and every service-to-service call as an edge, worked out
// from spans (a child span in another service than its parent is a call), over the page's time
// range. Node colour by error rate, label by requests a minute; dots travel along each edge.
// Clicking a service opens its panel: what it runs on, requests / errors / duration over time,
// and its operations.
import { useEffect, useMemo, useRef, useState } from "react";
import { records, sql } from "../api";
import type { Ctx } from "../App";
import { Tabs } from "../components/Charts";
import { Loads } from "../components/Panel";
import { Loaded } from "../useQuery";
import { bucketSeconds, fmtMs, fmtNum, rangeWindow } from "../time";

type Node = { service: string; requests: number; errors: number; avg_ms: number; attrs: Record<string, string>; x: number; y: number };
type Edge = { source: string; target: string; calls: number; errors: number };

// A span that starts work in its service: a server or consumer span, or a root.
const ENTRY = "(kind IN (2, 5) OR parent_span_id IS NULL)";
const ATTRS: [string, string][] = [
  ["namespace", "service.namespace"], ["version", "service.version"], ["language", "telemetry.sdk.language"],
  ["runtime", "process.runtime.name"], ["runtime_version", "process.runtime.version"],
  ["k8s_namespace", "k8s.namespace.name"], ["k8s_deployment", "k8s.deployment.name"], ["k8s_pod", "k8s.pod.name"],
  ["cloud", "cloud.provider"], ["account", "cloud.account.id"], ["region", "cloud.region"], ["zone", "cloud.availability_zone"],
  ["host", "host.name"],
];
const NODES_SQL = `SELECT service, count(*) FILTER (WHERE ${ENTRY}) AS requests,
  count(*) FILTER (WHERE ${ENTRY} AND status_code = 2) AS errors,
  avg(duration_ns) FILTER (WHERE ${ENTRY}) / 1e6 AS avg_ms,
  ${ATTRS.map(([k, a]) => `any_value(resource_attributes['${a}']) AS ${k}`).join(", ")}
FROM spans GROUP BY service`;
const EDGES_SQL = `SELECT p.service AS source, c.service AS target, count(*) AS calls,
  count(*) FILTER (WHERE c.status_code = 2) AS errors
FROM spans c JOIN spans p ON c.trace_id = p.trace_id AND c.parent_span_id = p.span_id
WHERE c.service <> p.service GROUP BY 1, 2`;
const q = (s: string) => `'${s.replace(/'/g, "''")}'`;

/** Runs one SQL query when `key` changes. */
function useSql(text: string | null, w: { start: string; end: string }, key: string): Loaded {
  const [state, setState] = useState<Loaded>({ data: null, error: null, loading: !!text });
  useEffect(() => {
    if (!text) return;
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    sql({ sql: text, ...w }).then((data) => live && setState({ data, error: null, loading: false }),
      (e: Error) => live && setState({ data: null, error: e.message, loading: false }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

const errPct = (n: { requests: number; errors: number }) => (n.requests ? (n.errors / n.requests) * 100 : 0);
const health = (pct: number) => (pct >= 5 ? "critical" : pct >= 1 ? "warning" : "ok");
const perMin = (n: number, minutes: number) => `${fmtNum(n / Math.max(minutes, 1))}/m`;

/** Columns by call depth (longest path from a service nobody calls), rows ordered by their callers. */
function layout(nodes: Omit<Node, "x" | "y">[], edges: Edge[]): Node[] {
  const out = new Map<string, string[]>(), into = new Map<string, string[]>();
  for (const e of edges) { out.set(e.source, [...(out.get(e.source) ?? []), e.target]); into.set(e.target, [...(into.get(e.target) ?? []), e.source]); }
  const depth = new Map<string, number>();
  const visit = (s: string, d: number, path: Set<string>) => {
    if ((depth.get(s) ?? -1) >= d || path.has(s)) return;
    depth.set(s, d);
    path.add(s); for (const t of out.get(s) ?? []) visit(t, d + 1, path); path.delete(s);
  };
  const roots = nodes.filter((n) => !(into.get(n.service)?.length)).map((n) => n.service);
  (roots.length ? roots : nodes.slice(0, 1).map((n) => n.service)).forEach((r) => visit(r, 0, new Set()));
  nodes.forEach((n) => depth.has(n.service) || visit(n.service, 0, new Set()));
  const cols: string[][] = [];
  for (const n of [...nodes].sort((a, b) => b.requests - a.requests)) (cols[depth.get(n.service)!] ??= []).push(n.service);
  const row = new Map<string, number>();
  cols.forEach((col, ci) => {
    if (ci > 0) col.sort((a, b) => avg(into.get(a), row) - avg(into.get(b), row));
    col.forEach((s, i) => row.set(s, i - (col.length - 1) / 2));
  });
  return nodes.map((n) => ({ ...n, x: 160 + depth.get(n.service)! * 260, y: row.get(n.service)! * 120 }));
}
const avg = (xs: string[] | undefined, row: Map<string, number>) => {
  const v = (xs ?? []).map((x) => row.get(x)).filter((x): x is number => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0;
};
const curve = (x1: number, y1: number, x2: number, y2: number) => {
  const mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
};

export function ServiceMap({ ctx }: { ctx: Ctx }) {
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const key = ctx.range.key + ctx.tick;
  const nq = useSql(NODES_SQL, w, "n" + key), eq = useSql(EDGES_SQL, w, "e" + key);
  const [sel, setSel] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const minutes = ctx.range.minutes;

  const { nodes, edges } = useMemo(() => {
    if (!nq.data || !eq.data) return { nodes: [] as Node[], edges: [] as Edge[] };
    const edges = records(eq.data).map((r) => ({ source: String(r.source), target: String(r.target), calls: Number(r.calls), errors: Number(r.errors) }));
    const raw = records(nq.data).map((r) => ({
      service: String(r.service), requests: Number(r.requests), errors: Number(r.errors), avg_ms: Number(r.avg_ms ?? 0),
      attrs: Object.fromEntries(ATTRS.map(([k]) => [k, r[k] == null ? "" : String(r[k])])),
    }));
    return { nodes: layout(raw, edges), edges };
  }, [nq.data, eq.data]);
  const selected = nodes.find((n) => n.service === sel) ?? null;

  return (
    <div className={`svcmap${selected ? " with-panel" : ""}`}>
      <div className="svcmap-main">
        <div className="toolbar">
          <input className="input grow" placeholder="Search services…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Search services" />
          <span className="faint">{nodes.length ? `${nodes.length} services · ${edges.length} connections · requests a minute` : ""}</span>
        </div>
        <Loads q={nq.error ? nq : eq} empty={!nodes.length} height={520}>
          {() => <Graph nodes={nodes} edges={edges} minutes={minutes} selected={sel} filter={filter.trim().toLowerCase()} onPick={setSel} />}
        </Loads>
      </div>
      {selected && <ServicePanel ctx={ctx} node={selected} minutes={minutes} edges={edges} onClose={() => setSel(null)} />}
    </div>
  );
}

function Graph(p: { nodes: Node[]; edges: Edge[]; minutes: number; selected: string | null; filter: string; onPick: (s: string | null) => void }) {
  const at = new Map(p.nodes.map((n) => [n.service, n]));
  const xs = p.nodes.map((n) => n.x), ys = p.nodes.map((n) => n.y);
  const box = { x: Math.min(...xs) - 160, y: Math.min(...ys) - 90, w: Math.max(...xs) - Math.min(...xs) + 320, h: Math.max(...ys) - Math.min(...ys) + 190 };
  const [view, setView] = useState(box);
  const fit = () => setView(box);
  useEffect(fit, [p.nodes.length]);   // eslint-disable-line react-hooks/exhaustive-deps
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const still = typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const zoom = (f: number) => setView((v) => ({ x: v.x + (v.w * (1 - f)) / 2, y: v.y + (v.h * (1 - f)) / 2, w: v.w * f, h: v.h * f }));
  const entries = p.nodes.filter((n) => !p.edges.some((e) => e.target === n.service));
  const dim = (s: string) => !!p.filter && !s.toLowerCase().includes(p.filter);
  const maxCalls = Math.max(1, ...p.edges.map((e) => e.calls));

  return (
    <div className="svcmap-canvas">
      <svg ref={svg} viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`} role="img" aria-label="Service map: services and the calls between them"
           onWheel={(e) => zoom(e.deltaY > 0 ? 1.12 : 0.89)}
           onPointerDown={(e) => { drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }; }}
           onPointerMove={(e) => {
             const d = drag.current, el = svg.current;
             if (!d || !el) return;
             const k = view.w / el.clientWidth;
             setView((v) => ({ ...v, x: d.vx - (e.clientX - d.x) * k, y: d.vy - (e.clientY - d.y) * k }));
           }}
           onPointerUp={() => { drag.current = null; }} onPointerLeave={() => { drag.current = null; }}>
        {entries.map((n) => {
          const d = `M${box.x},${n.y} L${n.x - 30},${n.y}`;
          return <g key={"in" + n.service} className={dim(n.service) ? "dim" : undefined}><path className="svc-edge" d={d} />{!still && <Dots d={d} n={2} err={0} />}</g>;
        })}
        {p.edges.map((e) => {
          const a = at.get(e.source), b = at.get(e.target);
          if (!a || !b) return null;
          const d = curve(a.x + 30, a.y, b.x - 30, b.y);
          const n = 1 + Math.round((Math.log10(e.calls + 1) / Math.log10(maxCalls + 1)) * 3);
          return (
            <g key={e.source + ">" + e.target} className={dim(e.source) && dim(e.target) ? "dim" : undefined}>
              <path className={`svc-edge${e.errors ? " err" : ""}`} d={d}><title>{`${e.source} → ${e.target}: ${fmtNum(e.calls)} calls${e.errors ? `, ${fmtNum(e.errors)} failed` : ""}`}</title></path>
              {!still && <Dots d={d} n={n} err={e.calls ? e.errors / e.calls : 0} />}
            </g>
          );
        })}
        {p.nodes.map((n) => (
          <g key={n.service} className={`svc-node ${health(errPct(n))}${p.selected === n.service ? " sel" : ""}${dim(n.service) ? " dim" : ""}`}
             transform={`translate(${n.x},${n.y})`} onClick={(e) => { e.stopPropagation(); p.onPick(p.selected === n.service ? null : n.service); }}
             tabIndex={0} role="button" aria-label={`${n.service}: ${perMin(n.requests, p.minutes)}, ${errPct(n).toFixed(1)}% errors`}
             onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); p.onPick(n.service); } }}>
            <rect x={-30} y={-30} width={60} height={60} rx={10} />
            <text className="svc-rate" y={4} textAnchor="middle">{perMin(n.requests, p.minutes)}</text>
            <text className="svc-name" y={50} textAnchor="middle">{n.service.length > 22 ? n.service.slice(0, 21) + "…" : n.service}</text>
          </g>
        ))}
      </svg>
      <div className="svcmap-zoom">
        <button className="btn" onClick={() => zoom(0.8)} aria-label="Zoom in">+</button>
        <button className="btn" onClick={() => zoom(1.25)} aria-label="Zoom out">−</button>
        <button className="btn" onClick={fit}>Fit</button>
      </div>
      <div className="svcmap-key faint"><i className="ok" />errors under 1% <i className="warning" />1-5% <i className="critical" />5% or more</div>
    </div>
  );
}

/** Dots travelling along an edge; some red, in proportion to the calls that failed. */
function Dots({ d, n, err }: { d: string; n: number; err: number }) {
  return (
    <>
      {Array.from({ length: n }, (_, i) => (
        <circle key={i} r={3} className={(i + 1) / n <= err * 4 || (err > 0 && i === 0) ? "svc-dot err" : "svc-dot"}>
          <animateMotion dur="3.2s" repeatCount="indefinite" begin={`${(i * 3.2) / n}s`} path={d} />
        </circle>
      ))}
    </>
  );
}

// telemetry.sdk.language -> its runtime dashboard
const RUNTIME_DASHBOARDS: Record<string, [string, string]> = { java: ["builtin-jvm", "JVM dashboard"], nodejs: ["builtin-nodejs", "Node.js dashboard"] };

function ServicePanel({ ctx, node, minutes, edges, onClose }: { ctx: Ctx; node: Node; minutes: number; edges: Edge[]; onClose: () => void }) {
  const [tab, setTab] = useState<"overview" | "operations">("overview");
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const b = bucketSeconds(ctx.range);
  const key = node.service + ctx.range.key + ctx.tick;
  const series = useSql(`SELECT time_bucket(INTERVAL '${b} seconds', ts) AS t, count(*) AS n,
      count(*) FILTER (WHERE status_code = 2) AS e, avg(duration_ns) / 1e6 AS ms
    FROM spans WHERE service = ${q(node.service)} AND ${ENTRY} GROUP BY 1 ORDER BY 1`, w, "s" + key);
  const ops = useSql(tab === "operations" ? `SELECT name, count(*) AS n, count(*) FILTER (WHERE status_code = 2) AS e,
      avg(duration_ns) / 1e6 AS avg_ms, quantile_cont(duration_ns, 0.95) / 1e6 AS p95_ms
    FROM spans WHERE service = ${q(node.service)} AND ${ENTRY} GROUP BY 1 ORDER BY 2 DESC LIMIT 50` : null, w, "o" + key + tab);
  const rows = series.data ? records(series.data) : [];
  const pct = errPct(node), h = health(pct), a = node.attrs;
  const calls = edges.filter((e) => e.source === node.service).map((e) => e.target);
  const callers = edges.filter((e) => e.target === node.service).map((e) => e.source);
  const sections: [string, [string, string][]][] = [
    ["Service", [["Namespace", a.namespace], ["Name", node.service], ["Version", a.version]]],
    ["Runtime", [["Language", a.language], ["Runtime", a.runtime], ["Version", a.runtime_version]]],
    ["Kubernetes", [["Namespace", a.k8s_namespace], ["Deployment", a.k8s_deployment], ["Pod", a.k8s_pod]]],
    ["Cloud", [["Provider", a.cloud], ["Account", a.account], ["Region", a.region], ["Zone", a.zone]]],
    ["Host", [["Name", a.host]]],
    ["Calls", [["Called by", callers.join(", ")], ["Calls", calls.join(", ")]]],
  ];

  return (
    <aside className="svc-panel" aria-label={`Service ${node.service}`}>
      <div className="svc-panel-head">
        <div>
          {a.namespace && <div className="faint mono">service.namespace = {a.namespace}</div>}
          <div className="svc-panel-title"><span className="faint">Service</span> <b>{node.service}</b></div>
        </div>
        <span className={`svc-badge ${h}`}>{h === "ok" ? "Healthy" : h === "warning" ? "Warning" : "Critical"}</span>
        <button className="linkbtn" onClick={onClose} aria-label="Close">✕</button>
      </div>
      <Tabs tabs={[["overview", "Overview"], ["operations", "Operations"]]} active={tab} onPick={setTab} />
      {tab === "overview" ? (
        <div className="svc-panel-body">
          <div className="svc-tiles">
            <Tile label="Requests" value={fmtNum(node.requests)} sub={perMin(node.requests, minutes)} pts={rows.map((r) => Number(r.n))} bars />
            <Tile label="Error rate" value={`${pct.toFixed(2)}%`} sub={`${fmtNum(node.errors)} failed`} pts={rows.map((r) => (Number(r.n) ? (Number(r.e) / Number(r.n)) * 100 : 0))} bad={pct >= 1} />
            <Tile label="Duration avg" value={fmtMs(node.avg_ms * 1e6)} sub="per request" pts={rows.map((r) => Number(r.ms ?? 0))} />
          </div>
          <div className="svc-summary">
            {sections.filter(([, kv]) => kv.some(([, v]) => v)).map(([title, kv]) => (
              <div key={title} className="svc-sum-row">
                <span className="svc-sum-title">{title}</span>
                <div className="svc-sum-kv">
                  {kv.filter(([, v]) => v).map(([k, v]) => <div key={k}><span className="faint">{k}</span><span className="mono">{v}</span></div>)}
                </div>
              </div>
            ))}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn" onClick={() => ctx.go(`/traces?service=${encodeURIComponent(node.service)}`)}>View its traces</button>
            {RUNTIME_DASHBOARDS[a.language] &&
              <button className="btn" onClick={() => ctx.go(`/dashboards/${RUNTIME_DASHBOARDS[a.language][0]}?service_name=${encodeURIComponent(node.service)}`)}>
                {RUNTIME_DASHBOARDS[a.language][1]}</button>}
            {a.host && <button className="btn" onClick={() => ctx.go(`/dashboards/builtin-hosts?host_name=${encodeURIComponent(a.host)}`)}>Host dashboard</button>}
          </div>
        </div>
      ) : (
        <div className="svc-panel-body">
          <Loads q={ops} empty={!(ops.data && ops.data.rows.length)} height={160}>
            {() => (
              <table className="dtable">
                <thead><tr><th>Operation</th><th className="num">Requests</th><th className="num">Errors</th><th className="num">Avg</th><th className="num">p95</th></tr></thead>
                <tbody>{records(ops.data!).map((r) => (
                  <tr key={String(r.name)}>
                    <td className="mono" title={String(r.name)}>{String(r.name)}</td>
                    <td className="num">{fmtNum(Number(r.n))}</td>
                    <td className={`num${Number(r.e) ? " bad" : ""}`}>{fmtNum(Number(r.e))}</td>
                    <td className="num">{fmtMs(Number(r.avg_ms) * 1e6)}</td>
                    <td className="num">{fmtMs(Number(r.p95_ms) * 1e6)}</td>
                  </tr>
                ))}</tbody>
              </table>
            )}
          </Loads>
        </div>
      )}
    </aside>
  );
}

function Tile({ label, value, sub, pts, bars, bad }: { label: string; value: string; sub: string; pts: number[]; bars?: boolean; bad?: boolean }) {
  const max = Math.max(1e-9, ...pts), W = 120, H = 34;
  const x = (i: number) => (pts.length > 1 ? (i / (pts.length - 1)) * W : W / 2);
  const y = (v: number) => H - (v / max) * (H - 2);
  return (
    <div className="svc-tile">
      <span className="faint">{label}</span>
      <b className={bad ? "bad" : undefined}>{value}</b>
      <span className="faint">{sub}</span>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        {pts.length > 0 && (bars
          ? pts.map((v, i) => <rect key={i} className="spark-bar" x={(i * W) / pts.length} width={Math.max(1, W / pts.length - 1.5)} y={y(v)} height={H - y(v)} />)
          : <polyline className={`spark-line${bad ? " bad" : ""}`} points={pts.map((v, i) => `${x(i)},${y(v)}`).join(" ")} />)}
      </svg>
    </div>
  );
}
