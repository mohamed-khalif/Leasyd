// Kubernetes: clusters, nodes, workloads, pods and events, from Leasyd's collectors
// (examples/kubernetes: kubeletstats, k8s_cluster, k8s_events). Tabs: Overview (health at a glance,
// what needs attention, usage by namespace), Nodes, Workloads, Pods, Events. A pod opens in a
// drawer (?pod=namespace/name): its containers, usage against limits, events, logs and traces.
// Queries: infra/queries.ts; contract: docs/gpu-kubernetes.md.
import { useMemo, useState } from "react";
import type { Ctx } from "../App";
import { Capped } from "../components/Capped";
import { Card, Drawer, Tabs } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { TimeSeries } from "../components/TimeSeries";
import { ago, bytes, cores, pct } from "../infra/format";
import { BAD_REASONS, PHASES } from "../infra/model";
import * as Q from "../infra/queries";
import { loads, useHint, useRows } from "../infra/useInfra";
import { bucketSeconds, fmtNum } from "../time";
import { useRangeWindow } from "../useNow";

type Tab = "overview" | "nodes" | "workloads" | "pods" | "events";
const TABS: [Tab, string][] = [["overview", "Overview"], ["nodes", "Nodes"], ["workloads", "Workloads"], ["pods", "Pods"], ["events", "Events"]];
const COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];

export function Kubernetes({ ctx, params }: { ctx: Ctx; params: URLSearchParams }) {
  const w = useRangeWindow(ctx);
  const key = ctx.range.key + ctx.tick;
  const hint = useHint(w, key);
  const [tab, setTab] = useState<Tab>((params.get("tab") as Tab) || "overview");
  const [cluster, setCluster] = useState("");
  const [namespace, setNamespace] = useState("");
  const [pod, setPod] = useState<string | null>(params.get("pod"));
  const f: Q.Filter = { cluster: cluster || null, namespace: namespace || null };
  const k = key + cluster + "|" + namespace;
  const nodes = useRows<Q.NodeRow>(Q.k8sNodes({ cluster: f.cluster }), w, "nodes" + key + cluster, hint);
  const pods = useRows<Q.PodRow>(Q.k8sPods(f), w, "pods" + k, hint);
  const clusters = [...new Set(nodes.rows.map((n) => n.cluster))].filter(Boolean).sort();
  const namespaces = useMemo(() => [...new Set(pods.rows.map((p) => p.namespace))].sort(), [pods.rows]);

  if (!nodes.loading && !nodes.error && !nodes.rows.length && !pods.rows.length && !cluster) return <Empty />;
  const filters = (
    <span style={{ display: "flex", gap: 8 }}>
      {(clusters.length > 1 || cluster) && (
        <select className="select" value={cluster} onChange={(e) => setCluster(e.target.value)} aria-label="Cluster">
          <option value="">All clusters</option>{clusters.map((c) => <option key={c}>{c}</option>)}
        </select>)}
      {tab !== "nodes" && (
        <select className="select" value={namespace} onChange={(e) => setNamespace(e.target.value)} aria-label="Namespace">
          <option value="">All namespaces</option>{(namespace && !namespaces.includes(namespace) ? [namespace, ...namespaces] : namespaces).map((n) => <option key={n}>{n}</option>)}
        </select>)}
    </span>
  );
  const props = { ctx, w, k, hint, f, nodes, pods, onPod: setPod };
  return (
    <div className="infra">
      <Tabs tabs={TABS} active={tab} onPick={setTab} right={filters} />
      {tab === "overview" && <Overview {...props} onTab={setTab} />}
      {tab === "nodes" && <Nodes {...props} />}
      {tab === "workloads" && <Workloads {...props} />}
      {tab === "pods" && <Pods {...props} />}
      {tab === "events" && <Events {...props} />}
      {pod && <PodDrawer ctx={ctx} w={w} hint={hint} id={pod} row={pods.rows.find((p) => `${p.namespace}/${p.pod}` === pod)} onClose={() => setPod(null)} />}
    </div>
  );
}

type Props = { ctx: Ctx; w: { start: string; end: string }; k: string; hint: ReturnType<typeof useHint>; f: Q.Filter;
  nodes: ReturnType<typeof useRows<Q.NodeRow>>; pods: ReturnType<typeof useRows<Q.PodRow>>; onPod: (id: string) => void };

function Empty() {
  return (
    <div className="infra">
      <Panel title="Kubernetes monitoring">
        <div style={{ padding: 16 }}>
          <p>No Kubernetes data in this time range yet.</p>
          <p className="muted">One command installs Leasyd's collectors in your cluster (Helm): <a href="#/start?source=kubernetes">Get started › Kubernetes</a>.
            You then see every node, workload and pod, restarts and crash loops, events, pod logs, and your GPUs.</p>
        </div>
      </Panel>
    </div>
  );
}

/** What's wrong with a pod, if anything: a bad container reason, Pending, Failed, or restarts in the range. */
export function podProblem(p: Q.PodRow): string | null {
  const bad = p.reason.split(", ").filter((r) => BAD_REASONS.includes(r));
  if (bad.length) return bad.join(", ");
  if (p.phase === 1) return "Pending";
  if (p.phase === 4) return "Failed";
  if ((p.restarts ?? 0) > 0) return `${fmtNum(p.restarts!)} restart${p.restarts === 1 ? "" : "s"}`;
  return null;
}

// ------------------------------------------------------------------ overview

function Overview({ ctx, w, k, hint, f, nodes, pods, onPod, onTab }: Props & { onTab: (t: Tab) => void }) {
  const b = bucketSeconds(ctx.range);
  const usage = useRows<Q.UsageSeriesRow>(Q.k8sUsageSeries(b, "namespace", f), w, "usage" + k, hint);
  const workloads = useRows<Q.WorkloadRow>(Q.k8sWorkloads(f), w, "wl" + k, hint);
  const events = useRows<Q.EventRow>(Q.k8sEvents(f), w, "ev" + k, hint);
  const ns = nodes.rows, ps = pods.rows;
  const ready = ns.filter((n) => n.ready === 1).length;
  const phase = (x: number) => ps.filter((p) => p.phase === x).length;
  const problems = ps.map((p) => [p, podProblem(p)] as const).filter(([, why]) => why);
  const sum = (xs: (number | null)[]) => xs.reduce<number>((s, x) => s + (x ?? 0), 0);
  const cpu = sum(ns.map((n) => n.cpu)), allocCpu = sum(ns.map((n) => n.alloc_cpu));
  const mem = sum(ns.map((n) => n.mem)), allocMem = sum(ns.map((n) => n.alloc_mem));
  const short = workloads.rows.filter((r) => r.kind !== "Job" && r.desired != null && (r.ready ?? 0) < r.desired);
  const warnings = events.rows.filter((e) => e.warning);
  const top = topKeys(usage.rows, (r) => r.cpu ?? 0, 6);
  const series = (field: "cpu" | "mem") => top.map((key, i) => ({ label: key, color: COLORS[i % COLORS.length],
    points: usage.rows.filter((r) => r.key === key && r[field] != null).map((r) => [Date.parse(r.t), Number(r[field])] as [number, number]) }));
  return (
    <>
      <div className="cards" style={{ gridTemplateColumns: "repeat(6, minmax(0, 1fr))" }}>
        <Card label="Nodes" value={`${ready} of ${ns.length}`} sub="ready" tone={ns.length && ready < ns.length ? "bad" : undefined} onClick={() => onTab("nodes")} />
        <Card label="Pods" value={fmtNum(ps.length)} sub={`${phase(2)} running · ${phase(1)} pending · ${phase(4)} failed`} onClick={() => onTab("pods")}
              tone={phase(1) || phase(4) ? "warn" : undefined} />
        <Card label="Pods with problems" value={String(problems.length)} sub="crash loops, image pulls, OOM kills, restarts, pending"
              tone={problems.length ? "bad" : "ok"} />
        <Card label="CPU" value={allocCpu ? pct((cpu / allocCpu) * 100) : "–"} sub={`${cores(cpu)} of ${cores(allocCpu)} allocatable`} />
        <Card label="Memory" value={allocMem ? pct((mem / allocMem) * 100) : "–"} sub={`${bytes(mem)} of ${bytes(allocMem)}`} />
        <Card label="Warning events" value={fmtNum(sum(warnings.map((e) => e.events)))} sub={`${warnings.length} kind${warnings.length === 1 ? "" : "s"} of trouble`} onClick={() => onTab("events")}
              tone={warnings.length ? "warn" : undefined} />
      </div>

      <Panel title="Needs attention" flush right={<span style={{ display: "flex", gap: 12 }}>
        <a href={`#/alerts/rules/new?template=${encodeURIComponent("Pod crash-looping")}`}>Alert me on crash loops ›</a>
        <a href={`#/alerts/rules/new?template=${encodeURIComponent("Node not ready")}`}>on nodes ›</a></span>}>
        <Loads q={loads(pods)} empty={false} height={80}>
          {() => problems.length || short.length || ns.some((n) => n.ready !== 1) ? (
            <table className="dtable">
              <thead><tr><th style={{ width: "34%" }}>What</th><th style={{ width: "18%" }}>Namespace</th><th>Why</th><th>Node</th></tr></thead>
              <tbody>
                {ns.filter((n) => n.ready !== 1).map((n) => (
                  <tr key={"n" + n.node}><td>node {n.node}</td><td className="faint">–</td><td className="bad">{n.ready === 0 ? "not ready" : "status unknown"}</td><td /></tr>
                ))}
                {short.map((r) => (
                  <tr key={"w" + r.namespace + r.name}><td>{r.kind} {r.name}</td><td>{r.namespace}</td>
                    <td className="warn">{fmtNum(r.ready ?? 0)} of {fmtNum(r.desired ?? 0)} ready</td><td /></tr>
                ))}
                {problems.slice(0, 50).map(([p, why]) => (
                  <tr key={p.namespace + p.pod} onClick={() => onPod(`${p.namespace}/${p.pod}`)}>
                    <td title={p.pod}>{p.pod}</td><td>{p.namespace}</td>
                    <td className={/Restart|restart/.test(why!) ? "warn" : "bad"}>{why}</td><td className="faint" title={p.node}>{p.node || "–"}</td></tr>
                ))}
              </tbody>
            </table>
          ) : <div className="state">All nodes ready, all workloads at their replicas, no pod in trouble.</div>}
        </Loads>
      </Panel>

      <div className="grid2">
        <Panel title="CPU by namespace">
          <Loads q={loads(usage)} empty={!usage.rows.length} height={170}>
            {() => <TimeSeries range={ctx.range} height={170} format={(v) => cores(v)} series={series("cpu")} />}
          </Loads>
        </Panel>
        <Panel title="Memory by namespace">
          <Loads q={loads(usage)} empty={!usage.rows.length} height={170}>
            {() => <TimeSeries range={ctx.range} height={170} format={(v) => bytes(v)} series={series("mem")} />}
          </Loads>
        </Panel>
      </div>
    </>
  );
}

function topKeys<T extends { key: string }>(rows: T[], v: (r: T) => number, n: number): string[] {
  const tot = new Map<string, number>();
  for (const r of rows) tot.set(r.key, (tot.get(r.key) ?? 0) + v(r));
  return [...tot.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([key]) => key);
}

function Bar({ used, total }: { used: number | null; total: number | null }) {
  if (used == null || !total) return <span className="faint">–</span>;
  const p = (used / total) * 100;
  return (
    <span className="util" title={`${p.toFixed(1)}%`}>
      <span className="util-bar"><span style={{ width: `${Math.min(100, p)}%` }} className={p >= 90 ? "hot" : ""} /></span>
      <span>{Math.round(p)}%</span>
    </span>
  );
}

// ------------------------------------------------------------------ nodes

function Nodes({ nodes }: Props) {
  return (
    <Panel title="Nodes" flush>
      <Loads q={loads(nodes)} empty={!nodes.rows.length} height={160}>
        {() => (
          <Capped rows={nodes.rows}>{(rows) => (<table className="dtable">
            <thead><tr><th style={{ width: "24%" }}>Node</th><th>Cluster</th><th>Instance</th><th>Status</th><th className="num">CPU</th>
              <th className="num">Memory</th><th className="num">Disk</th><th className="num">Pods</th><th className="num">GPUs</th></tr></thead>
            <tbody>{rows.map((n) => (
              <tr key={n.node}>
                <td title={n.node}>{n.node}</td><td>{n.cluster || "–"}</td>
                <td className="faint" title={n.zone}>{n.instance_type || "–"}</td>
                <td className={n.ready === 1 ? "ok" : "bad"}>{n.ready === 1 ? "Ready" : n.ready === 0 ? "Not ready" : "Unknown"}</td>
                <td className="num" title={`${cores(n.cpu)} of ${cores(n.alloc_cpu)}`}><Bar used={n.cpu} total={n.alloc_cpu} /></td>
                <td className="num" title={`${bytes(n.mem)} of ${bytes(n.alloc_mem)}`}><Bar used={n.mem} total={n.alloc_mem} /></td>
                <td className="num"><Bar used={n.fs_used} total={n.fs_cap} /></td>
                <td className="num">{n.pods}{n.alloc_pods ? <span className="faint"> / {n.alloc_pods}</span> : null}</td>
                <td className="num">{n.gpus ? <a href="#/gpus">{n.gpus}</a> : <span className="faint">–</span>}</td>
              </tr>
            ))}</tbody>
          </table>)}</Capped>
        )}
      </Loads>
    </Panel>
  );
}

// ------------------------------------------------------------------ workloads

function Workloads({ w, k, hint, f, pods }: Props) {
  const wl = useRows<Q.WorkloadRow>(Q.k8sWorkloads(f), w, "wl" + k, hint);
  const usage = new Map<string, { cpu: number; mem: number; restarts: number; pods: number }>();
  for (const p of pods.rows) {
    const id = `${p.namespace}/${p.workload}`;
    const u = usage.get(id) ?? { cpu: 0, mem: 0, restarts: 0, pods: 0 };
    u.cpu += p.cpu ?? 0; u.mem += p.mem ?? 0; u.restarts += p.restarts ?? 0; u.pods += 1;
    usage.set(id, u);
  }
  return (
    <Panel title="Workloads" flush right={<span className="faint">usage: the sum of their pods' latest</span>}>
      <Loads q={loads(wl)} empty={!wl.rows.length} height={160}>
        {() => (
          <Capped rows={wl.rows}>{(rows) => (<table className="dtable">
            <thead><tr><th style={{ width: "26%" }}>Workload</th><th>Kind</th><th>Namespace</th><th className="num">Ready</th>
              <th className="num">CPU</th><th className="num">Memory</th><th className="num">Restarts</th></tr></thead>
            <tbody>{rows.map((r) => {
              const u = usage.get(`${r.namespace}/${r.name}`);
              const ok = r.kind === "Job" || r.desired == null || (r.ready ?? 0) >= r.desired;
              return (
                <tr key={r.namespace + r.kind + r.name}>
                  <td title={r.name}>{r.name}</td><td className="faint">{r.kind}</td><td>{r.namespace}</td>
                  <td className={`num${ok ? "" : " warn"}`}>{r.kind === "Job" ? `${fmtNum(r.desired ?? 0)} active` : `${fmtNum(r.ready ?? 0)} / ${fmtNum(r.desired ?? 0)}`}</td>
                  <td className="num">{u ? cores(u.cpu) : "–"}</td><td className="num">{u ? bytes(u.mem) : "–"}</td>
                  <td className={`num${u?.restarts ? " warn" : ""}`}>{u ? fmtNum(u.restarts) : "–"}</td>
                </tr>
              );
            })}</tbody>
          </table>)}</Capped>
        )}
      </Loads>
    </Panel>
  );
}

// ------------------------------------------------------------------ pods

function Pods({ pods, onPod }: Props) {
  const [text, setText] = useState("");
  const [only, setOnly] = useState(false);
  const shown = pods.rows.filter((p) => (!text || p.pod.includes(text) || p.workload.includes(text)) && (!only || podProblem(p)));
  return (
    <Panel title={`Pods (${fmtNum(shown.length)})`} flush right={
      <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <label className="faint" style={{ display: "flex", gap: 4, alignItems: "center" }}><input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} /> problems only</label>
        <input className="input" placeholder="Search pods…" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search pods" />
      </span>}>
      <Loads q={loads(pods)} empty={!shown.length} height={160}>
        {() => (
          <Capped rows={shown}>{(rows) => (<table className="dtable">
            <thead><tr><th style={{ width: "27%" }}>Pod</th><th>Namespace</th><th>Status</th><th style={{ width: "16%" }}>Node</th>
              <th className="num">CPU</th><th className="num">Memory</th><th className="num">Of limit</th><th className="num">Restarts</th></tr></thead>
            <tbody>{rows.map((p) => {
              const why = podProblem(p);
              return (
                <tr key={p.namespace + p.pod} onClick={() => onPod(`${p.namespace}/${p.pod}`)}>
                  <td title={p.pod}>{p.pod}{p.workload && <div className="faint">{p.kind} {p.workload}</div>}</td>
                  <td>{p.namespace}</td>
                  <td className={why ? (why.includes("restart") ? "warn" : "bad") : "ok"}>{why ?? PHASES[p.phase ?? 0] ?? "–"}</td>
                  <td className="faint" title={p.node}>{p.node || "–"}</td>
                  <td className="num">{cores(p.cpu)}</td><td className="num">{bytes(p.mem)}</td>
                  <td className={`num${(p.mem_limit ?? 0) >= 0.9 ? " bad" : ""}`} title="memory as a share of its limit">{p.mem_limit == null ? "–" : pct(p.mem_limit * 100)}</td>
                  <td className={`num${p.restarts ? " warn" : ""}`}>{p.restarts == null ? "–" : fmtNum(p.restarts)}</td>
                </tr>
              );
            })}</tbody>
          </table>)}</Capped>
        )}
      </Loads>
    </Panel>
  );
}

// ------------------------------------------------------------------ events

function Events({ w, k, hint, f, onPod }: Props) {
  const ev = useRows<Q.EventRow>(Q.k8sEvents(f), w, "ev" + k, hint);
  return (
    <Panel title="Events" flush right={<span className="faint">grouped by object and reason; warnings first</span>}>
      <Loads q={loads(ev)} empty={!ev.rows.length} height={160}>
        {() => <EventTable rows={ev.rows} onPod={onPod} />}
      </Loads>
    </Panel>
  );
}

function EventTable({ rows, onPod }: { rows: Q.EventRow[]; onPod?: (id: string) => void }) {
  return (
    <table className="dtable">
      <thead><tr><th style={{ width: 120 }}>Last</th><th style={{ width: "14%" }}>Reason</th><th style={{ width: "22%" }}>Object</th>
        <th>Message</th><th className="num">Count</th></tr></thead>
      <tbody>{rows.map((e) => (
        <tr key={e.namespace + e.object + e.reason} onClick={() => onPod && e.kind === "Pod" && onPod(`${e.namespace}/${e.object}`)}>
          <td className="faint">{ago(e.last)}</td>
          <td className={e.warning ? "warn" : ""}>{e.reason}</td>
          <td title={e.object}>{e.object}<div className="faint">{e.kind}{e.namespace ? ` · ${e.namespace}` : ""}</div></td>
          <td style={{ whiteSpace: "normal", wordBreak: "break-word" }}>{e.message}</td>
          <td className="num">{fmtNum(e.events)}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

// ------------------------------------------------------------------ one pod

/** Kubernetes' own short forms: 200m, 1.5 (cores); 410Mi, 2Gi. */
const cpuShort = (v: number | null) => (v == null ? "–" : v < 1 ? `${Math.round(v * 1000)}m` : `${+v.toFixed(2)}`);
const memShort = (v: number | null) => (v == null ? "–" : v >= 2 ** 30 ? `${+(v / 2 ** 30).toFixed(1)}Gi` : `${Math.round(v / 2 ** 20)}Mi`);

function PodDrawer({ ctx, w, hint, id, row, onClose }: { ctx: Ctx; w: { start: string; end: string }; hint: ReturnType<typeof useHint>;
  id: string; row?: Q.PodRow; onClose: () => void }) {
  const [ns, name] = [id.split("/")[0], id.split("/").slice(1).join("/")];
  const b = bucketSeconds(ctx.range);
  const k = id + ctx.tick;
  const series = useRows<Q.PodSeriesRow>(Q.k8sPodSeries(ns, name, b), w, "ps" + k, hint);
  const containers = useRows<Q.ContainerRow>(Q.k8sContainers(ns, name), w, "pc" + k, hint);
  const events = useRows<Q.EventRow>(Q.k8sEvents({}, { namespace: ns, name }), w, "pe" + k, hint);
  const pts = (key: keyof Q.PodSeriesRow, scale = 1) => series.rows.filter((r) => r[key] != null).map((r) => [Date.parse(r.t), Number(r[key]) * scale] as [number, number]);
  const why = row ? podProblem(row) : null;
  return (
    <Drawer title={<>{name} <span className="faint">{ns}</span></>} onClose={onClose}>
      <div className="infra-drawer">
      {row && (
        <dl className="kv">
          <dt>Status</dt><dd className={why ? "bad" : ""}>{why ?? PHASES[row.phase ?? 0] ?? "–"}</dd>
          {row.workload && <><dt>Workload</dt><dd>{row.kind} {row.workload}</dd></>}
          {row.team && <><dt>Team</dt><dd>{row.team}</dd></>}
          <dt>Node</dt><dd>{row.node || "not scheduled"}{row.cluster && <span className="faint"> · {row.cluster}</span>}</dd>
          <dt>Now</dt><dd>{cores(row.cpu)} CPU · {bytes(row.mem)} memory{row.mem_limit != null && ` (${pct(row.mem_limit * 100)} of its limit)`}</dd>
        </dl>
      )}
      <p style={{ display: "flex", gap: 14 }}>
        <a href={`#/logs?q=${encodeURIComponent(name)}`}>Logs ›</a>
        {row?.workload && <a href={`#/traces?service=${encodeURIComponent(row.workload)}`}>Traces of {row.workload} ›</a>}
      </p>
      <h4>Containers</h4>
      <Loads q={loads(containers)} empty={!containers.rows.length} height={60}>
        {() => (
          <Capped rows={containers.rows}>{(rows) => (<table className="dtable">
            <thead><tr><th style={{ width: "30%" }}>Container</th><th>State</th><th className="num">Restarts</th>
              <th className="num" title="request / limit">CPU</th><th className="num" title="request / limit">Memory</th></tr></thead>
            <tbody>{rows.map((c) => (
              <tr key={c.container}>
                <td title={c.image}>{c.container}<div className="faint">{c.image}</div></td>
                <td className={c.reason ? "bad" : c.ready === 1 ? "ok" : "warn"}>{c.reason || (c.ready === 1 ? "ready" : "not ready")}</td>
                <td className={`num${c.restarts ? " warn" : ""}`}>{fmtNum(c.restarts ?? 0)}<span className="faint"> ({fmtNum(c.restarts_total ?? 0)} total)</span></td>
                <td className="num" title="request / limit">{cpuShort(c.cpu_request)} / {c.cpu_limit == null ? "–" : cpuShort(c.cpu_limit)}</td>
                <td className="num" title="request / limit">{memShort(c.mem_request)} / {c.mem_limit == null ? "–" : memShort(c.mem_limit)}</td>
              </tr>
            ))}</tbody>
          </table>)}</Capped>
        )}
      </Loads>
      <h4>CPU and memory</h4>
      <Loads q={loads(series)} empty={!series.rows.length} height={120}>
        {() => (
          <>
            <TimeSeries range={ctx.range} height={110} format={(v) => cores(v)} series={[{ label: "CPU", color: "var(--series-1)", points: pts("cpu") }]} />
            <TimeSeries range={ctx.range} height={110} format={(v) => bytes(v)} series={[{ label: "memory", color: "var(--series-2)", points: pts("mem") }]} />
            {series.rows.some((r) => r.mem_limit != null) && (
              <TimeSeries range={ctx.range} height={110} unit="%" thresholds={[{ value: 100, color: "var(--sev-error)" }]} series={[
                { label: "memory of limit", color: "var(--series-5)", points: pts("mem_limit", 100) },
                { label: "CPU of limit", color: "var(--series-4)", points: pts("cpu_limit", 100) }].filter((s) => s.points.length)} />
            )}
          </>
        )}
      </Loads>
      <h4>Events</h4>
      <Loads q={loads(events)} empty={!events.rows.length} height={60}>
        {() => <EventTable rows={events.rows} />}
      </Loads>
      </div>
    </Drawer>
  );
}
