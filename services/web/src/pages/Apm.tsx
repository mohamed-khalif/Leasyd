// APM: every service the account's apps and the eBPF agent report (lib/services.ts, the same data as
// the service map), as a table: requests, error rate and duration, healthiest last. A service's page
// shows those over time, its operations, what calls it and what it calls, and its latest errors,
// with links to its traces, logs and dashboards.
import { useMemo, useState } from "react";
import { fmtMs, fmtNum, fmtPercent, fmtTime } from "../lib/format";
import { sqlString as q } from "../lib/sql";
import { records } from "../api";
import type { Ctx } from "../App";
import { StackedBars, Tabs } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { TimeSeries } from "../components/TimeSeries";
import { useHashParam } from "../hashParams";
import { useSql } from "../useQuery";
import { useRangeWindow } from "../useNow";
import { OUTSIDE } from "../lib/serviceGraph";
import { type Edge, ENTRY, errPct, health, perMin, RUNTIME_DASHBOARDS, type Svc, useServiceDetail, useServices } from "../lib/services";
import { Tile } from "../components/Tile";

const HEALTH_LABEL = { ok: "Healthy", warning: "Warning", critical: "Critical" } as const;
const source = (s: Svc) => (s.kind !== "service" ? "its callers (eBPF)" : s.ebpf && s.sdk ? "eBPF + SDK" : s.ebpf ? "eBPF" : "SDK");

export function Apm({ ctx, service }: { ctx: Ctx; service?: string }) {
  return service ? <ServiceView ctx={ctx} service={service} /> : <ServiceList ctx={ctx} />;
}

// ------------------------------------------------------------------ all services

type SortKey = "service" | "requests" | "errors" | "avg_ms";

function ServiceList({ ctx }: { ctx: Ctx }) {
  const { nodes, loaded } = useServices(ctx);
  const [filter, setFilter] = useHashParam("q");
  const [sort, setSort] = useState<SortKey>("requests");
  const minutes = ctx.range.minutes;
  const services = useMemo(() => nodes.filter((n) => n.kind === "service" && n.service !== OUTSIDE), [nodes]);
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const by = (n: Svc) => (sort === "errors" ? errPct(n) : sort === "service" ? 0 : n[sort]);
    return services.filter((n) => n.service.toLowerCase().includes(f))
      .sort((a, b) => (sort === "service" ? a.service.localeCompare(b.service) : by(b) - by(a)));
  }, [services, filter, sort]);
  const total = services.reduce((t, n) => ({ requests: t.requests + n.requests, errors: t.errors + n.errors }), { requests: 0, errors: 0 });
  const unhealthy = services.filter((n) => health(errPct(n)) !== "ok").length;
  const th = (k: SortKey, label: string, num = true) => (
    <th className={num ? "num" : undefined} aria-sort={sort === k ? "descending" : undefined}>
      <button className="linkbtn" onClick={() => setSort(k)}>{label}{sort === k ? " ▾" : ""}</button>
    </th>
  );

  return (
    <div className="apm">
      <div className="apm-summary">
        <div><span className="faint">Services</span><b>{fmtNum(services.length)}</b></div>
        <div><span className="faint">Requests</span><b>{fmtNum(total.requests)}</b><span className="faint">{perMin(total.requests, minutes)}</span></div>
        <div><span className="faint">Error rate</span><b className={errPct(total) >= 1 ? "bad" : undefined}>{fmtPercent(errPct(total))}</b></div>
        <div><span className="faint">Need attention</span><b className={unhealthy ? "bad" : undefined}>{fmtNum(unhealthy)}</b></div>
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <input className="input grow" placeholder="Search services…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Search services" />
        <a className="btn" href="#/services">Service map</a>
      </div>
      <Panel title="Services" flush>
        <Loads q={loaded} empty={!services.length} height={200}>
          {() => (
            <table className="dtable">
              <thead><tr>{th("service", "Service", false)}{th("requests", "Requests")}<th className="num">Rate</th>
                {th("errors", "Error rate")}{th("avg_ms", "Duration avg")}<th>Language</th><th>Seen by</th><th>Health</th></tr></thead>
              <tbody>{shown.map((n) => {
                const h = health(errPct(n));
                return (
                  <tr key={n.service} onClick={() => ctx.go(`/apm/${encodeURIComponent(n.service)}`)}>
                    <td title={n.service}>{n.service}</td>
                    <td className="num">{fmtNum(n.requests)}</td>
                    <td className="num">{perMin(n.requests, minutes)}</td>
                    <td className={`num${h !== "ok" ? " bad" : ""}`}>{fmtPercent(errPct(n))}</td>
                    <td className="num">{fmtMs(n.avg_ms * 1e6)}</td>
                    <td>{n.attrs.language || n.attrs.runtime}</td>
                    <td className="faint">{source(n)}</td>
                    <td><span className={`svc-badge ${h}`}>{HEALTH_LABEL[h]}</span></td>
                  </tr>
                );
              })}</tbody>
            </table>
          )}
        </Loads>
      </Panel>
      {!services.length && !loaded.loading && !loaded.error && (
        <p className="faint">
          No services in this time range. Install the agent from <a href="#/start?source=server">Get started › Server</a> or{" "}
          <a href="#/start?source=kubernetes">Kubernetes</a>: with eBPF it sees every app on the machine or cluster, any language,
          with no code changes. Or add your language's OpenTelemetry agent (<a href="#/start?source=java">Java</a>,{" "}
          <a href="#/start?source=nodejs">Node.js</a>, <a href="#/start?source=python">Python</a>, <a href="#/start?source=ruby">Ruby</a>).
        </p>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ one service

type Tab = "operations" | "dependencies" | "errors";

function ServiceView({ ctx, service }: { ctx: Ctx; service: string }) {
  const { nodes, edges, loaded } = useServices(ctx);
  const node = nodes.find((n) => n.service === service);
  return (
    <div className="apm">
      <div><button className="linkbtn" onClick={() => ctx.go("/apm")}>← All services</button></div>
      {node ? <ServiceDetail ctx={ctx} node={node} edges={edges} /> : (
        <Loads q={loaded} height={200}>
          {() => <p className="faint">No data for <b>{service}</b> in this time range. Try a longer range.</p>}
        </Loads>
      )}
    </div>
  );
}

function ServiceDetail({ ctx, node, edges }: { ctx: Ctx; node: Svc; edges: Edge[] }) {
  const [tab, setTab] = useHashParam("tab", "operations") as [Tab, (t: Tab) => void];
  const d = useServiceDetail(ctx, node, tab === "operations");
  const a = d.attrs, minutes = ctx.range.minutes;
  const pct = errPct(node), h = health(pct);
  const pts = d.points;
  const p95 = pts.filter((p) => p.p95 != null);
  const go = (path: string) => () => ctx.go(path);
  const svc = encodeURIComponent(node.service);
  const facts = ([["Version", a.version], ["Language", a.language], ["Runtime", [a.runtime, a.runtime_version].filter(Boolean).join(" ")],
    ["Kubernetes", [a.k8s_namespace, a.k8s_deployment].filter(Boolean).join(" / ")], ["Host", a.host],
    ["Cloud", [a.cloud, a.region].filter(Boolean).join(" ")], ["Seen by", source(node)]] as [string, string][]).filter(([, v]) => v);

  return (
    <>
      <div className="apm-head">
        <div>
          {a.namespace && <div className="faint mono">service.namespace = {a.namespace}</div>}
          <h2 className="apm-title">{node.service} <span className={`svc-badge ${h}`}>{HEALTH_LABEL[h]}</span></h2>
          <div className="apm-facts">{facts.map(([k, v]) => <span key={k}><span className="faint">{k}</span> <span className="mono">{v}</span></span>)}</div>
        </div>
        <div className="apm-links">
          {node.kind === "service" && <button className="btn" onClick={go(`/traces?service=${svc}`)}>Traces</button>}
          {node.sdk && <button className="btn" onClick={go(`/logs?service=${svc}`)}>Logs</button>}
          <button className="btn" onClick={go("/services")}>Service map</button>
          {node.sdk && RUNTIME_DASHBOARDS[a.language] &&
            <button className="btn" onClick={go(`/dashboards/${RUNTIME_DASHBOARDS[a.language][0]}?service_name=${svc}`)}>{RUNTIME_DASHBOARDS[a.language][1]}</button>}
          {a.k8s_namespace && <button className="btn" onClick={go(`/kubernetes?tab=workloads&namespace=${encodeURIComponent(a.k8s_namespace)}`)}>Kubernetes</button>}
          {a.host && <button className="btn" onClick={go(`/dashboards/builtin-hosts?host_name=${encodeURIComponent(a.host)}`)}>Host</button>}
          {a.platform === "aws_lambda" && <button className="btn" onClick={go(`/lambda/${svc}`)}>Lambda function</button>}
        </div>
      </div>
      <div className="svc-tiles">
        <Tile label="Requests" value={fmtNum(node.requests)} sub={perMin(node.requests, minutes)} pts={pts.map((p) => p.n)} bars />
        <Tile label="Error rate" value={fmtPercent(pct)} sub={`${fmtNum(node.errors)} failed`} pts={pts.map((p) => (p.n ? (p.e / p.n) * 100 : 0))} bad={pct >= 1} />
        <Tile label="Duration avg" value={fmtMs(node.avg_ms * 1e6)} sub="per request" pts={pts.map((p) => p.ms)} />
      </div>
      <div className="lambda-charts">
        <Panel title="Requests and errors">
          <Loads q={d.seriesQ} empty={!pts.length} height={150}>
            {() => <StackedBars range={ctx.range} bucketMs={d.bucketS * 1000}
                                keys={[{ label: "Errors", color: "var(--sev-error)" }, { label: "OK", color: "var(--text-3)" }]}
                                bars={pts.map((p) => ({ t: p.t, values: [p.e, Math.max(0, p.n - p.e)] }))} />}
          </Loads>
        </Panel>
        <Panel title="Error rate">
          <Loads q={d.seriesQ} empty={!pts.length} height={150}>
            {() => <TimeSeries range={ctx.range} height={150} format={(v) => fmtPercent(v)} threshold={1}
                               series={[{ label: "errors", color: "var(--sev-error)", points: pts.map((p) => [p.t, p.n ? (p.e / p.n) * 100 : 0] as [number, number]) }]} />}
          </Loads>
        </Panel>
        <Panel title="Duration">
          <Loads q={d.seriesQ} empty={!pts.length} height={150}>
            {() => <TimeSeries range={ctx.range} height={150} format={(v) => fmtMs(v * 1e6)}
                               series={[{ label: "average", color: "var(--series-1)", points: pts.map((p) => [p.t, p.ms] as [number, number]) },
                                        ...(p95.length ? [{ label: "p95", color: "var(--series-3)", points: p95.map((p) => [p.t, p.p95!] as [number, number]) }] : [])]} />}
          </Loads>
        </Panel>
      </div>
      <Tabs tabs={[["operations", "Operations"], ["dependencies", "Dependencies"], ["errors", "Latest errors"]]} active={tab} onPick={setTab} />
      {tab === "operations" && (
        <Panel title="Operations" flush>
          <Loads q={d.opsQ} empty={!d.opRows.length} height={160}>
            {() => (
              <table className="dtable">
                <thead><tr><th style={{ width: "40%" }}>Operation</th><th className="num">Requests</th><th className="num">Rate</th><th className="num">Error rate</th>
                  <th className="num">Avg</th>{!d.ebpf && <th className="num">p95</th>}</tr></thead>
                <tbody>{d.opRows.map((r) => (
                  <tr key={r.name} onClick={d.ebpf ? undefined : go(`/traces?service=${svc}&q=${encodeURIComponent(r.name)}`)}>
                    <td className="mono" title={r.name}>{r.name}</td>
                    <td className="num">{fmtNum(r.n)}</td>
                    <td className="num">{perMin(r.n, minutes)}</td>
                    <td className={`num${r.n && r.e / r.n >= 0.01 ? " bad" : ""}`}>{fmtPercent(r.n ? (r.e / r.n) * 100 : 0)}</td>
                    <td className="num">{fmtMs(r.avg_ms * 1e6)}</td>
                    {!d.ebpf && <td className="num">{fmtMs((r.p95_ms ?? 0) * 1e6)}</td>}
                  </tr>
                ))}</tbody>
              </table>
            )}
          </Loads>
        </Panel>
      )}
      {tab === "dependencies" && (
        <div className="lambda-charts apm-deps">
          <DependencyTable ctx={ctx} title="Called by" rows={edges.filter((e) => e.target === node.service)} peer={(e) => e.source} />
          <DependencyTable ctx={ctx} title="Calls" rows={edges.filter((e) => e.source === node.service)} peer={(e) => e.target} />
        </div>
      )}
      {tab === "errors" && <LatestErrors ctx={ctx} node={node} sampled={d.ebpf} />}
    </>
  );
}

function DependencyTable({ ctx, title, rows, peer }: { ctx: Ctx; title: string; rows: Edge[]; peer: (e: Edge) => string }) {
  const sorted = [...rows].sort((a, b) => b.calls - a.calls);
  return (
    <Panel title={title} flush>
      {sorted.length ? (
        <table className="dtable">
          <thead><tr><th style={{ width: "45%" }}>Service</th><th className="num">Calls</th><th className="num">Failed</th><th className="num">Error rate</th></tr></thead>
          <tbody>{sorted.map((e) => {
            const name = peer(e), rate = e.calls ? (e.errors / e.calls) * 100 : 0;
            return (
              <tr key={name} onClick={name === OUTSIDE ? undefined : () => ctx.go(`/apm/${encodeURIComponent(name)}`)}>
                <td title={name}>{name}</td>
                <td className="num">{fmtNum(e.calls)}</td>
                <td className={`num${e.errors ? " bad" : ""}`}>{fmtNum(e.errors)}</td>
                <td className={`num${rate >= 1 ? " bad" : ""}`}>{fmtPercent(rate)}</td>
              </tr>
            );
          })}</tbody>
        </table>
      ) : <div className="state faint" style={{ minHeight: 80 }}>None in this time range</div>}
    </Panel>
  );
}

function LatestErrors({ ctx, node, sampled }: { ctx: Ctx; node: Svc; sampled: boolean }) {
  const w = useRangeWindow(ctx);
  const errs = useSql(`SELECT ts, trace_id, name, duration_ns, status_message FROM spans
    WHERE service = ${q(node.service)} AND ${ENTRY} AND status_code = 2 ORDER BY ts DESC LIMIT 50`, w, "x" + node.service + ctx.range.key + ctx.tick);
  const list = errs.data ? records(errs.data) : [];
  return (
    <Panel title="Latest errors" flush>
      <Loads q={errs} empty={!list.length} height={160}>
        {() => (
          <table className="dtable">
            <thead><tr><th style={{ width: 170 }}>Time</th><th style={{ width: "30%" }}>Operation</th><th className="num" style={{ width: 100 }}>Duration</th><th>Message</th></tr></thead>
            <tbody>{list.map((r, i) => (
              <tr key={String(r.trace_id) + i} onClick={() => ctx.go(`/traces/${r.trace_id}`)}>
                <td>{fmtTime(String(r.ts), ctx.range)}</td>
                <td className="mono" title={String(r.name)}>{String(r.name)}</td>
                <td className="num">{fmtMs(Number(r.duration_ns))}</td>
                <td className="bad" title={String(r.status_message ?? "")}>{String(r.status_message ?? "")}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Loads>
      {sampled && <p className="faint" style={{ padding: "0 12px 12px" }}>Only traces the eBPF agent kept are listed (it samples them; the numbers above count every request).</p>}
    </Panel>
  );
}
