// GPUs: NVIDIA GPU fleet health, usage and cost, from dcgm-exporter (Kubernetes or plain servers;
// examples/kubernetes, setup.sh --gpu). Tabs: Overview (the fleet at a glance, models, every GPU),
// Workloads & cost (who uses them, who holds them idle, what to do), Health (XIDs, ECC, throttling,
// heat) and Planning (daily peaks, never-needed GPUs, the demand trend). A GPU opens in a drawer.
// Queries: infra/queries.ts; conclusions: infra/analysis.ts; contract: docs/gpu-kubernetes.md.
import { useState } from "react";
import { fmtAgo, fmtDateTime, fmtDuration, fmtFixed, fmtHours, fmtMiB, fmtNum, fmtPercent, fmtUsd, shortGpu } from "../lib/format";
import type { Ctx } from "../App";
import { Capped } from "../components/Capped";
import { Card, Drawer, StackedBars, Tabs } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { TimeSeries } from "../components/TimeSeries";
import { byModel, byTeam, fleetSpend, forecast, now, recommendations } from "../infra/analysis";
import { HOT_TEMP, IDLE_UTIL, priceFor, xidInfo, type GpuPrice } from "../infra/model";
import * as Q from "../infra/queries";
import { loads, useGpuPrices, useHint, useRows } from "../infra/useInfra";
import { useRangeWindow } from "../useNow";

type Tab = "overview" | "workloads" | "health" | "planning";
const TABS: [Tab, string][] = [["overview", "Overview"], ["workloads", "Workloads & cost"], ["health", "Health"], ["planning", "Planning"]];
const USE_KEYS = [{ label: "busy", color: "var(--accent)" }, { label: "held, idle", color: "var(--sev-warn)" },
                  { label: "unallocated", color: "var(--series-6)" }];

export function Gpus({ ctx, params }: { ctx: Ctx; params: URLSearchParams }) {
  const w = useRangeWindow(ctx);
  const key = ctx.range.key + ctx.tick;
  const hint = useHint(w, key);
  const [tab, setTab] = useState<Tab>((params.get("tab") as Tab) || "overview");
  const [cluster, setCluster] = useState<string>("");
  const [gpu, setGpu] = useState<string | null>(params.get("gpu"));
  const f: Q.Filter = { cluster: cluster || null };
  const k = key + cluster;
  const fleet = useRows<Q.GpuRow>(Q.gpuFleet(ctx.range.minutes, f), w, "fleet" + k, hint);
  const { prices, error: pricesError } = useGpuPrices(ctx.tick);
  const clusters = [...new Set(fleet.rows.map((g) => g.cluster))].filter(Boolean).sort();

  if (!fleet.loading && !fleet.error && !fleet.rows.length && !cluster) return <Empty />;
  return (
    <div className="infra">
      <Tabs tabs={TABS} active={tab} onPick={setTab} right={
        (clusters.length > 1 || cluster) ? (
          <select className="select" value={cluster} onChange={(e) => setCluster(e.target.value)} aria-label="Cluster">
            <option value="">All clusters</option>
            {clusters.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>) : null} />
      {pricesError && <div className="form-error" role="alert">Couldn't read your GPU prices ({pricesError}): costs below use list prices.</div>}
      {tab === "overview" && <Overview ctx={ctx} w={w} k={k} hint={hint} f={f} fleet={fleet} prices={prices} onGpu={setGpu} onTab={setTab} />}
      {tab === "workloads" && <Workloads ctx={ctx} w={w} k={k} hint={hint} f={f} fleet={fleet} prices={prices} />}
      {tab === "health" && <Health ctx={ctx} w={w} k={k} hint={hint} f={f} onGpu={setGpu} />}
      {tab === "planning" && <Planning ctx={ctx} w={w} k={k} hint={hint} f={f} fleet={fleet} prices={prices} />}
      {gpu && <GpuDrawer ctx={ctx} w={w} hint={hint} gpu={gpu} row={fleet.rows.find((g) => g.gpu === gpu)} onClose={() => setGpu(null)} />}
    </div>
  );
}

type Props = { ctx: Ctx; w: { start: string; end: string }; k: string; hint: ReturnType<typeof useHint>; f: Q.Filter };

function Empty() {
  return (
    <div className="infra">
      <Panel title="GPU monitoring">
        <div style={{ padding: 16 }}>
          <p>No GPU data in this time range yet.</p>
          <p className="muted">Leasyd reads NVIDIA's dcgm-exporter: in Kubernetes, <a href="#/start?source=kubernetes">Get started › Kubernetes</a> (with
            <code> --gpu</code>); on a server, <a href="#/start?source=gpu">Get started › NVIDIA GPUs</a>. You then see each GPU's usage, memory,
            temperature, power and errors, which workload and team uses it, what it costs and what sits idle.</p>
        </div>
      </Panel>
    </div>
  );
}

// ------------------------------------------------------------------ overview

function Overview({ ctx, w, k, hint, f, fleet, prices, onGpu, onTab }: Props & { fleet: ReturnType<typeof useRows<Q.GpuRow>>;
  prices: GpuPrice[]; onGpu: (g: string) => void; onTab: (t: Tab) => void }) {
  const series = useRows<Q.GpuSeriesRow>(Q.gpuSeries(ctx.range.minutes, f), w, "series" + k, hint);
  const health = useRows<Q.GpuHealthRow>(Q.gpuHealth(f), w, "health" + k, hint);
  const gs = fleet.rows;
  const spend = fleetSpend(gs, prices), state = now(gs), models = byModel(gs, prices);
  const utilAvg = weightedUtil(gs);
  const perHour = gs.reduce((s, g) => s + priceFor(g.model, prices).usd, 0);
  const b = Q.gpuBucketSeconds(ctx.range.minutes);
  const bars = series.rows.map((r) => ({ t: Date.parse(r.t), values: [r.active, r.idle, r.free] }));
  const issues = health.rows.length;
  return (
    <>
      <div className="cards" style={{ gridTemplateColumns: "repeat(6, minmax(0, 1fr))" }}>
        <Card label="GPUs" value={fmtNum(gs.length)} sub={models.map((m) => `${m.gpus} × ${m.label.replace(/^NVIDIA /, "")}`).join(" · ") || "–"} />
        <Card label="Busy now" value={`${state.active} of ${gs.length}`} sub={`${state.idle} held but idle · ${state.free} unallocated`}
              tone={state.idle ? "warn" : undefined} />
        <Card label="Utilization" value={fmtPercent(utilAvg)} sub="average over the range" />
        <Card label="Estimated spend" value={fmtUsd(spend.total)} sub={`in the range · ${fmtUsd(perHour)} an hour now`} />
        <Card label="Idle spend" value={fmtUsd(spend.idle + spend.free)} onClick={() => onTab("workloads")}
              sub={`${fmtUsd(spend.idle)} held idle · ${fmtUsd(spend.free)} unallocated`} tone={spend.idle + spend.free > spend.total * 0.2 ? "warn" : undefined} />
        <Card label="Hardware issues" value={health.loading ? "…" : String(issues)} onClick={() => onTab("health")}
              sub={issues ? "GPUs with XIDs, ECC errors, throttling or heat" : "none in this range"} tone={issues ? "bad" : "ok"} />
      </div>

      <Panel title="Your GPU fleet over time" right={<span className="faint">GPUs busy (≥ {IDLE_UTIL}% utilization), held by a workload but idle, or unallocated</span>}>
        <Loads q={loads(series)} empty={!bars.length} height={160}>
          {() => <StackedBars bars={bars} range={ctx.range} bucketMs={b * 1000} keys={USE_KEYS} height={160} />}
        </Loads>
      </Panel>

      <div className="grid2">
        <Panel title="Utilization and memory">
          <Loads q={loads(series)} empty={!series.rows.length} height={160}>
            {() => <TimeSeries range={ctx.range} height={160} unit="%" series={[
              { label: "GPU utilization", color: "var(--series-1)", points: series.rows.map((r) => [Date.parse(r.t), r.util]) },
              { label: "memory used", color: "var(--series-2)", points: series.rows.filter((r) => r.mem != null).map((r) => [Date.parse(r.t), (r.mem ?? 0) * 100]) },
            ]} />}
          </Loads>
        </Panel>
        <Panel title="Device distribution" flush>
          <Loads q={loads(fleet)} empty={!models.length} height={160}>
            {() => (
              <table className="dtable">
                <thead><tr><th style={{ width: "36%" }}>Model</th><th className="num">GPUs</th><th className="num">Nodes</th>
                  <th className="num" style={{ width: 110 }}>Busy</th><th className="num">$ / GPU hour</th></tr></thead>
                <tbody>{models.map((m) => (
                  <tr key={m.model}><td title={m.model}>{m.model}</td><td className="num">{m.gpus}</td><td className="num">{m.nodes}</td>
                    <td className="num"><UtilBar v={m.util} /></td>
                    <td className="num" title={m.own_price ? "your price (Settings)" : "AWS on-demand list price; set yours in Settings"}>
                      {fmtUsd(m.usd_per_hour)}{m.own_price ? "" : " *"}</td></tr>
                ))}</tbody>
              </table>
            )}
          </Loads>
          <p className="faint" style={{ padding: "6px 12px" }}>* list price estimate: set your own in <a href="#/settings">Settings › GPU prices</a>.</p>
        </Panel>
      </div>

      <Panel title="GPUs" flush right={<span className="faint">click one for its details</span>}>
        <Loads q={loads(fleet)} empty={!gs.length} height={160}>
          {() => (
            <Capped rows={gs}>{(rows) => (<table className="dtable">
              <thead><tr><th style={{ width: "19%" }}>Node</th><th style={{ width: 92 }}>GPU</th><th style={{ width: "13%" }}>Model</th>
                <th style={{ width: "21%" }}>Used by</th><th className="num" style={{ width: 110 }}>Now</th><th className="num">Average</th><th className="num">Memory</th>
                <th className="num">Temp max</th><th className="num">Idle</th></tr></thead>
              <tbody>{rows.map((g) => (
                <tr key={g.gpu} onClick={() => onGpu(g.gpu)}>
                  <td title={g.node}>{g.node}</td>
                  <td className="mono" title={g.gpu}>{shortGpu(g.gpu)}</td>
                  <td title={g.model}>{g.model.replace(/^NVIDIA /, "")}</td>
                  <td title={g.holder_now}>{g.holder_now ? <>{g.holder_now.split("/").pop()}{g.team_now && <span className="faint"> · {g.team_now}</span>}</>
                    : <span className="faint">unallocated</span>}</td>
                  <td className="num"><UtilBar v={g.util_now} /></td>
                  <td className="num">{fmtPercent(g.util_avg)}</td>
                  <td className="num" title={`${fmtMiB(g.mem_used_now)} of ${fmtMiB(g.mem_total)}`}>{memShort(g.mem_used_now, g.mem_total)}</td>
                  <td className={`num${(g.temp_max ?? 0) >= HOT_TEMP ? " bad" : ""}`}>{g.temp_max == null ? "–" : `${Math.round(g.temp_max)} °C`}</td>
                  <td className={`num${g.idle_hours > g.hours / 2 ? " warn" : ""}`}>{fmtHours(g.idle_hours + g.free_hours)}</td>
                </tr>
              ))}</tbody>
            </table>)}</Capped>
          )}
        </Loads>
      </Panel>
    </>
  );
}

function UtilBar({ v }: { v: number | null | undefined }) {
  if (v == null) return <span className="faint">–</span>;
  return (
    <span className="util">
      <span className="util-bar"><span style={{ width: `${Math.min(100, Math.max(0, v))}%` }} className={v < IDLE_UTIL ? "idle" : ""} /></span>
      <span>{Math.round(v)}%</span>
    </span>
  );
}

/** "13.7 / 22.5 GiB" from MiB. */
function memShort(used: number | null, total: number | null) {
  if (used == null) return "–";
  return total ? `${fmtMiB(used)} / ${fmtMiB(total)}` : fmtMiB(used);
}

function weightedUtil(gs: Q.GpuRow[]) {
  const h = gs.reduce((s, g) => s + g.hours, 0);
  return h ? gs.reduce((s, g) => s + g.util_avg * g.hours, 0) / h : null;
}

// ------------------------------------------------------------------ workloads & cost

function Workloads({ ctx, w, k, hint, f, fleet, prices }: Props & { fleet: ReturnType<typeof useRows<Q.GpuRow>>; prices: GpuPrice[] }) {
  const wl = useRows<Q.GpuWorkloadRow>(Q.gpuWorkloads(ctx.range.minutes, f), w, "wl" + k, hint);
  const stalled = useRows<Q.StalledRow>(Q.gpuStalled(f), w, "stalled" + k, hint);
  const rangeHours = (Date.parse(w.end) - Date.parse(w.start)) / 3_600_000;
  // hardware has its own tab (Health): here, what to do about usage and cost
  const recs = recommendations({ gpus: fleet.rows, workloads: wl.rows, stalled: stalled.rows, health: [], rangeHours, prices });
  const teams = byTeam(wl.rows, prices);
  const spend = fleetSpend(fleet.rows, prices);
  const held = wl.rows.reduce((s, r) => s + r.hours, 0);
  const heldUtil = held ? wl.rows.reduce((s, r) => s + r.util_avg * r.hours, 0) / held : null;
  return (
    <>
      <div className="cards" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
        <Card label="Estimated spend" value={fmtUsd(spend.total)} sub={`${fmtHours(fleet.rows.reduce((s, g) => s + g.hours, 0))} of GPU time in the range`} />
        <Card label="Held but idle" value={fmtUsd(spend.idle)} sub="workloads holding GPUs they don't use: reclaimable" tone={spend.idle ? "warn" : undefined} />
        <Card label="Unallocated" value={fmtUsd(spend.free)} sub="GPUs no workload held: spare capacity" />
        <Card label="Utilization of held GPUs" value={fmtPercent(heldUtil)} sub="how hard workloads use the GPUs they hold" />
      </div>

      <Panel title="Recommendations" flush right={<a href={`#/alerts/rules/new?template=${encodeURIComponent("GPU held but idle")}`}>Alert me when a GPU sits idle ›</a>}>
        <Loads q={loads(wl)} empty={false} height={80}>
          {() => recs.length ? (
            <ul className="recs">{recs.map((r, i) => (
              <li key={i} className={r.level}>
                <div className="rec-title">{r.title}{r.monthly_saving ? <span className="rec-save">≈ {fmtUsd(r.monthly_saving)} a month</span> : null}</div>
                <div className="faint">{r.detail}</div>
              </li>
            ))}</ul>
          ) : <div className="state">Nothing to reclaim: GPUs held in this range were used.</div>}
        </Loads>
      </Panel>

      <div className="grid2">
        <Panel title="Spend by team" flush right={<span className="faint">pod label <code>team</code>, else the namespace</span>}>
          <Loads q={loads(wl)} empty={!teams.length} height={120}>
            {() => (
              <table className="dtable">
                <thead><tr><th style={{ width: "30%" }}>Team</th><th className="num" style={{ width: 60 }}>GPUs</th><th className="num" style={{ width: 120 }}>Utilization</th>
                  <th className="num">Spend</th><th className="num" style={{ width: 100 }}>Idle spend</th></tr></thead>
                <tbody>{teams.map((t) => (
                  <tr key={t.team}><td>{t.team}</td><td className="num">{t.gpus}</td><td className="num"><UtilBar v={t.util} /></td>
                    <td className="num">{fmtUsd(t.spend)}</td><td className={`num${t.idle_spend > t.spend * 0.5 ? " warn" : ""}`}>{fmtUsd(t.idle_spend)}</td></tr>
                ))}</tbody>
              </table>
            )}
          </Loads>
        </Panel>
        <Panel title="Waiting for GPUs" flush right={<span className="faint">pods Kubernetes couldn't schedule for lack of GPUs</span>}>
          <Loads q={loads(stalled)} empty={false} height={120}>
            {() => stalled.rows.length ? (
              <table className="dtable">
                <thead><tr><th style={{ width: "40%" }}>Pod</th><th>Since</th><th className="num">Tries</th><th>Now</th></tr></thead>
                <tbody>{stalled.rows.map((s) => (
                  <tr key={s.namespace + s.pod} title={s.message} onClick={() => ctx.go(`/kubernetes?pod=${encodeURIComponent(`${s.namespace}/${s.pod}`)}`)}>
                    <td>{s.pod}<div className="faint">{s.namespace}</div></td><td className="faint">{fmtAgo(s.first)}</td>
                    <td className="num">{fmtNum(s.events)}</td>
                    <td className={s.phase === 1 ? "warn" : "faint"}>{s.phase === 1 ? "still pending" : s.phase === 2 ? "running" : "gone"}</td></tr>
                ))}</tbody>
              </table>
            ) : <div className="state">No pod waited for GPUs in this range.</div>}
          </Loads>
        </Panel>
      </div>

      <Panel title="GPU time by workload" flush>
        <Loads q={loads(wl)} empty={!wl.rows.length} height={140}>
          {() => (
            <Capped rows={wl.rows}>{(rows) => (<table className="dtable">
              <thead><tr><th style={{ width: "22%" }}>Workload</th><th>Namespace</th><th>Team</th><th style={{ width: "14%" }}>Model</th>
                <th className="num">GPUs</th><th className="num">Utilization</th><th className="num">GPU time</th><th className="num">Spend</th>
                <th className="num">Idle</th></tr></thead>
              <tbody>{rows.map((r) => {
                const p = priceFor(r.model, prices).usd, idleShare = r.hours ? r.idle_hours / r.hours : 0;
                return (
                  <tr key={r.namespace + r.workload + r.model}>
                    <td title={r.kind}>{r.workload}<span className="faint"> {r.kind}</span></td>
                    <td>{r.namespace}</td><td>{r.team || <span className="faint">–</span>}</td>
                    <td title={r.model}>{r.model.replace(/^NVIDIA /, "")}</td>
                    <td className="num">{r.gpus}{r.holding_now !== r.gpus ? <span className="faint"> ({r.holding_now} now)</span> : null}</td>
                    <td className="num"><UtilBar v={r.util_avg} /></td>
                    <td className="num">{fmtHours(r.hours)}</td>
                    <td className="num">{fmtUsd(r.hours * p)}</td>
                    <td className={`num${idleShare >= 0.8 ? " warn" : ""}`}>{fmtPercent(idleShare * 100)}</td>
                  </tr>
                );
              })}</tbody>
            </table>)}</Capped>
          )}
        </Loads>
      </Panel>
    </>
  );
}

// ------------------------------------------------------------------ health

function Health({ w, k, hint, f, onGpu }: Props & { onGpu: (g: string) => void }) {
  const health = useRows<Q.GpuHealthRow>(Q.gpuHealth(f), w, "health" + k, hint);
  const xids = useRows<Q.XidRow>(Q.gpuXids(f), w, "xids" + k, hint);
  const rs = health.rows;
  const count = (p: (r: Q.GpuHealthRow) => boolean) => rs.filter(p).length;
  const xidGpus = count((r) => !!r.xids?.length), ecc = count((r) => (r.dbe ?? 0) > 0 || (r.remap_failure ?? 0) > 0 || (r.uncorrectable_rows ?? 0) > 0);
  const throttled = count((r) => (r.thermal_s ?? 0) > 0 || (r.power_s ?? 0) > 0), hot = count((r) => (r.temp_max ?? 0) >= HOT_TEMP);
  return (
    <>
      <div className="cards" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
        <Card label="GPUs with XID errors" value={String(xidGpus)} tone={xidGpus ? "bad" : "ok"} sub="errors the driver reported" />
        <Card label="Memory errors" value={String(ecc)} tone={ecc ? "bad" : "ok"} sub="double-bit ECC, row remapping" />
        <Card label="Throttled" value={String(throttled)} tone={throttled ? "warn" : "ok"} sub="slowed for heat or power" />
        <Card label={`Hotter than ${HOT_TEMP} °C`} value={String(hot)} tone={hot ? "warn" : "ok"} sub="at some point in the range" />
      </div>
      <Panel title="GPUs that need attention" flush right={<span style={{ display: "flex", gap: 12 }}>
        <a href={`#/alerts/rules/new?template=${encodeURIComponent("GPU XID error")}`}>Alert me on XID errors ›</a>
        <a href={`#/alerts/rules/new?template=${encodeURIComponent("GPU too hot")}`}>on heat ›</a></span>}>
        <Loads q={loads(health)} empty={false} height={140}>
          {() => rs.length ? (
            <Capped rows={rs}>{(rows) => (<table className="dtable">
              <thead><tr><th style={{ width: "20%" }}>Node</th><th style={{ width: 80 }}>GPU</th><th style={{ width: "30%" }}>Signs</th>
                <th>Used by</th><th className="num">Temp max</th></tr></thead>
              <tbody>{rows.map((r) => (
                <tr key={r.gpu} onClick={() => onGpu(r.gpu)}>
                  <td title={r.node}>{r.node}<div className="faint">{r.model}</div></td>
                  <td className="mono">{shortGpu(r.gpu)}</td>
                  <td style={{ whiteSpace: "normal" }}><Signs r={r} /></td>
                  <td title={r.holder}>{r.holder || <span className="faint">–</span>}</td>
                  <td className={`num${(r.temp_max ?? 0) >= HOT_TEMP ? " bad" : ""}`}>{r.temp_max == null ? "–" : `${Math.round(r.temp_max)} °C`}</td>
                </tr>
              ))}</tbody>
            </table>)}</Capped>
          ) : <div className="state">No hardware signs in this range: no XIDs, ECC errors, throttling or overheating.</div>}
        </Loads>
      </Panel>
      <Panel title="XID errors" flush right={<span className="faint">what each means and what to do (NVIDIA's XID catalog)</span>}>
        <Loads q={loads(xids)} empty={false} height={100}>
          {() => xids.rows.length ? (
            <table className="dtable">
              <thead><tr><th style={{ width: 150 }}>When</th><th style={{ width: "18%" }}>GPU</th><th style={{ width: "26%" }}>XID</th><th>What to do</th></tr></thead>
              <tbody>{xids.rows.map((x) => {
                const info = xidInfo(x.xid);
                return (
                  <tr key={x.gpu + x.xid + x.first} onClick={() => onGpu(x.gpu)}>
                    <td className="faint">{fmtDateTime(x.last)}</td>
                    <td title={x.gpu}>{x.node}<div className="faint mono">{shortGpu(x.gpu)}{x.holder ? ` · ${x.holder}` : ""}</div></td>
                    <td className={info.level === "critical" ? "bad" : info.level === "warning" ? "warn" : ""} style={{ whiteSpace: "normal" }}>
                      XID {x.xid}: {info.title}</td>
                    <td style={{ whiteSpace: "normal" }}>{info.action}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          ) : <div className="state">No XID errors in this range.</div>}
        </Loads>
      </Panel>
    </>
  );
}

function Signs({ r }: { r: Q.GpuHealthRow }) {
  const out: [string, "bad" | "warn"][] = [];
  for (const x of r.xids ?? []) out.push([`XID ${x}: ${xidInfo(x).title}`, xidInfo(x).level === "critical" ? "bad" : "warn"]);
  if ((r.dbe ?? 0) > 0) out.push([`${r.dbe} double-bit ECC`, "bad"]);
  if ((r.remap_failure ?? 0) > 0) out.push(["row remapping failed", "bad"]);
  if ((r.uncorrectable_rows ?? 0) > 0) out.push([`${r.uncorrectable_rows} row${r.uncorrectable_rows === 1 ? "" : "s"} remapped`, "warn"]);
  if ((r.pcie_replays ?? 0) > 0) out.push([`${fmtNum(r.pcie_replays!)} PCIe replays`, "warn"]);
  if ((r.thermal_s ?? 0) > 0) out.push([`thermal throttling ${fmtSeconds(r.thermal_s!)}`, "warn"]);
  if ((r.power_s ?? 0) > 0) out.push([`power capping ${fmtSeconds(r.power_s!)}`, "warn"]);
  if ((r.temp_max ?? 0) >= HOT_TEMP && !(r.thermal_s ?? 0)) out.push([`up to ${Math.round(r.temp_max!)} °C`, "warn"]);
  return <>{out.map(([t, tone]) => <span key={t} className={`pill ${tone}`}>{t}</span>)}</>;
}

const fmtSeconds = (s: number) => fmtDuration(s * 1000);

// ------------------------------------------------------------------ planning

function Planning({ ctx, w, k, hint, f, fleet, prices }: Props & { fleet: ReturnType<typeof useRows<Q.GpuRow>>; prices: GpuPrice[] }) {
  const days = useRows<Q.GpuDayRow>(Q.gpuDays(ctx.range.minutes, f), w, "days" + k, hint);
  const fc = forecast(days.rows);
  // GPUs no workload held in the whole range, at their own prices
  const never = fleet.rows.filter((g) => g.hours > 0 && g.free_hours >= g.hours - 1e-9);
  const neverPerMonth = never.reduce((s, g) => s + priceFor(g.model, prices).usd, 0) * 730;
  const short = ctx.range.minutes < 2 * 1440;
  const day0 = days.rows.length ? Date.parse(String(days.rows[0].day)) : Date.now();
  const dayRange = { key: "days", label: "", minutes: (days.rows.length || 1) * 1440, from: day0, to: day0 + (days.rows.length || 1) * 86_400_000 };
  const pts = (key: keyof Q.GpuDayRow) => days.rows.map((d) => [Date.parse(String(d.day)) + 43_200_000, Number(d[key])] as [number, number]);
  const inDays = fc ? (fc.days === 1 ? "this day" : `these ${fc.days} days`) : "";
  return (
    <>
      {short && <div className="start-banner"><span>Planning looks at daily peaks: choose <b>Last 7 days</b> or <b>Last 30 days</b> in the time range.</span></div>}
      <div className="cards" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
        <Card label="GPUs in the fleet" value={fc ? String(fc.fleet) : "–"} sub="the most present at once" />
        <Card label="Most held at once" value={fc ? String(fc.peak_held) : "–"} sub={fc ? `${fc.peak_active} busy at the busiest moment` : ""} />
        <Card label="Never needed" value={fc ? String(fc.never_needed) : "–"} tone={fc?.never_needed ? "warn" : undefined}
              sub={fc?.never_needed ? (never.length ? `≈ ${fmtUsd(neverPerMonth)} a month if released` : "held at once at no moment") : "every GPU was held at some point"} />
        <Card label="Busy peak in 30 days" value={fc?.in_30_days == null ? "–" : String(fc.in_30_days)}
              sub={fc?.in_30_days == null ? "needs 3 days or more" : `trend ${fc.slope_per_day >= 0 ? "+" : ""}${fmtFixed(fc.slope_per_day, 2)} GPUs a day`}
              tone={fc?.in_30_days != null && fc.in_30_days > fc.fleet ? "bad" : undefined} />
      </div>
      <Panel title="Daily peaks" right={<span className="faint">the most GPUs present, held and busy at once each day; busy: 95th percentile too</span>}>
        <Loads q={loads(days)} empty={!days.rows.length} height={180}>
          {() => <TimeSeries range={dayRange} height={180} dots series={[
            { label: "present", color: "var(--series-6)", points: pts("gpus_max") },
            { label: "held", color: "var(--series-3)", points: pts("held_max") },
            { label: "busy (peak)", color: "var(--series-1)", points: pts("active_max") },
            { label: "busy (95th percentile)", color: "var(--series-4)", points: pts("active_p95") },
          ]} />}
        </Loads>
      </Panel>
      {fc && (
        <Panel title="What this means">
          <ul className="recs plain">
            <li>{fc.never_needed
              ? `At most ${fc.peak_held} of ${fc.fleet} GPUs were held at once in ${inDays}: the fleet could be ${fc.never_needed} GPU${fc.never_needed === 1 ? "" : "s"} smaller.`
              : `At some moment in ${inDays} every GPU was held.`}</li>
            <li>{`At the busiest moment ${fc.peak_active} GPU${fc.peak_active === 1 ? " was" : "s were"} busy, while ${fc.peak_held} ${fc.peak_held === 1 ? "was" : "were"} held: `
              + (fc.peak_held > fc.peak_active ? `${fc.peak_held - fc.peak_active} held but idle even then (see Workloads & cost).` : "workloads used what they held.")}</li>
            {fc.in_30_days != null && <li>{fc.in_30_days > fc.fleet
              ? `If the trend holds, busy peaks reach ${fc.in_30_days} GPUs in 30 days, more than the ${fc.fleet} you have: plan capacity (or reclaim idle GPUs) before then.`
              : `If the trend holds, busy peaks reach about ${fc.in_30_days} GPUs in 30 days, within the ${fc.fleet} you have.`}</li>}
          </ul>
        </Panel>
      )}
    </>
  );
}

// ------------------------------------------------------------------ one GPU

function GpuDrawer({ ctx, w, hint, gpu, row, onClose }: { ctx: Ctx; w: { start: string; end: string }; hint: ReturnType<typeof useHint>;
  gpu: string; row?: Q.GpuRow; onClose: () => void }) {
  const b = Math.max(60, Q.gpuBucketSeconds(ctx.range.minutes));
  const d = useRows<Q.GpuDetailRow>(Q.gpuDetail(gpu, b), w, "gpu" + gpu + ctx.tick, hint);
  const xids = useRows<Q.XidRow>(Q.gpuXids({}), w, "gx" + ctx.tick, hint);
  const mine = xids.rows.filter((x) => x.gpu === gpu);
  const s = (key: keyof Q.GpuDetailRow, scale = 1) => d.rows.filter((r) => r[key] != null).map((r) => [Date.parse(r.t), Number(r[key]) * scale] as [number, number]);
  const holder = row?.holder_now;
  return (
    <Drawer title={<>{row?.model ?? "GPU"} <span className="mono faint">{shortGpu(gpu)}</span></>} onClose={onClose}>
      <div className="infra-drawer">
      {row && (
        <dl className="kv">
          <dt>Node</dt><dd>{row.node}{row.instance_type && <span className="faint"> · {row.instance_type}</span>}</dd>
          {row.cluster && <><dt>Cluster</dt><dd>{row.cluster}</dd></>}
          <dt>UUID</dt><dd className="mono">{gpu}</dd>
          <dt>Used by</dt><dd>{holder ? <a href={`#/kubernetes?pod=${encodeURIComponent(holder)}`}>{holder}</a> : "unallocated"}{row.team_now && <span className="faint"> · team {row.team_now}</span>}</dd>
          <dt>In the range</dt><dd>{fmtHours(row.active_hours)} busy · {fmtHours(row.idle_hours)} held idle · {fmtHours(row.free_hours)} unallocated</dd>
        </dl>
      )}
      {mine.length > 0 && (
        <>
          <h4>Hardware errors</h4>
          <ul className="recs">{mine.map((x) => {
            const info = xidInfo(x.xid);
            return (
              <li key={x.xid + x.first} className={info.level}>
                <div className="rec-title">XID {x.xid}: {info.title}<span className="faint" style={{ fontWeight: 400 }}>{fmtAgo(x.last)}{x.holder ? ` · ${x.holder}` : ""}</span></div>
                <div className="faint">{info.action}</div>
              </li>
            );
          })}</ul>
        </>
      )}
      <Loads q={loads(d)} empty={!d.rows.length} height={160}>
        {() => (
          <>
            <h4>Utilization</h4>
            <TimeSeries range={ctx.range} height={140} unit="%" series={[
              { label: "GPU utilization", color: "var(--series-1)", points: s("util") },
              { label: "engine active", color: "var(--series-4)", points: s("engine", 100) },
              { label: "tensor cores", color: "var(--series-2)", points: s("tensor", 100) }].filter((x) => x.points.length)} />
            <h4>Memory used</h4>
            <TimeSeries range={ctx.range} height={120} format={(v) => fmtMiB(v)} series={[{ label: "memory used", color: "var(--series-3)", points: s("mem_used") }]} />
            <h4>Temperature and power</h4>
            <TimeSeries range={ctx.range} height={120} thresholds={[{ value: HOT_TEMP, color: "var(--sev-error)" }]} series={[
              { label: "temperature °C", color: "var(--sev-error)", points: s("temp") }, { label: "power W", color: "var(--series-5)", points: s("power") }]} />
          </>
        )}
      </Loads>
      {holder && !holder.startsWith("slurm/") && (
        <p><a href={`#/logs?q=${encodeURIComponent(holder.split("/")[1])}`}>Logs mentioning {holder.split("/")[1]} ›</a></p>
      )}
      </div>
    </Drawer>
  );
}
