import { FormEvent, useEffect, useMemo, useState } from "react";
import { Check, checks, notExcluded, Query, Result, Slo, slos, Where } from "../api";
import type { Ctx } from "../App";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { Stat } from "../components/Stat";
import { TimeSeries } from "../components/TimeSeries";
import { customRange, fmtNum } from "../time";
import { Loaded, useQuery } from "../useQuery";

// SLOs over synthetic checks, worked out from their results (the tenant's own metrics):
//   availability  good = runs that passed          (synthetics.check.success = 1)
//   performance   good = runs within threshold_ms  (synthetics.check.duration <= threshold)
// over the last window_days, leaving out excluded runs (maintenance windows, and runs excluded
// by hand). Error budget: the share of runs allowed to be bad, (100 - target)%.

const SUCCESS = "synthetics.check.success", DURATION = "synthetics.check.duration";
const OK = "var(--ok, #3fb68b)", WARN = "var(--sev-warn, #e0a030)", BAD = "var(--sev-error)";
const HOUR = 3_600_000, DAY = 86_400_000;

export function Slos({ ctx, path }: { ctx: Ctx; path: string }) {
  const [, , id, sub] = path.split("/");            // /slos[/new | /<id>[/edit]]
  if (id === "new") return <SloForm ctx={ctx} />;
  if (id && sub === "edit") return <EditSlo ctx={ctx} id={id} />;
  if (id) return <SloDetail ctx={ctx} id={id} />;
  return <SloList ctx={ctx} />;
}

function useChecks(tick: number) {
  const [list, setList] = useState<Check[] | null>(null);
  useEffect(() => { checks.list().then((d) => setList(d.checks), () => setList([])); }, [tick]);
  return list;
}

// ------------------------------------------------------------------ evaluation

export type SloStatus = { good: number; total: number; attainment: number | null; allowedBad: number; bad: number;
                          budgetLeft: number | null; burn1h: number | null; state: "healthy" | "at risk" | "breached" | "no data" };

/** The numbers of an SLO from its good and total runs (and the last hour's, for the burn rate). */
export function evaluate(target: number, good: number, total: number, good1h = 0, total1h = 0): SloStatus {
  const allowedFrac = (100 - target) / 100;
  const bad = total - good, allowedBad = total * allowedFrac;
  const attainment = total ? (good / total) * 100 : null;
  const budgetLeft = !total ? null : allowedBad > 0 ? 1 - bad / allowedBad : bad ? -Infinity : 1;
  const burn1h = total1h ? ((total1h - good1h) / total1h) / allowedFrac : null;
  const state = attainment == null ? "no data" : attainment < target ? "breached"
    : (budgetLeft ?? 1) < 0.25 || (burn1h ?? 0) > 2 ? "at risk" : "healthy";
  return { good, total, attainment, allowedBad, bad, budgetLeft, burn1h, state };
}

/** Queries for an SLO's good and total runs between start and end, optionally per time bucket. */
function sloQueries(slo: Slo, excluded: string[], start: number, end: number, bucket?: number): { good: Query; total: Query } {
  const base = { signal: "metrics" as const, start: new Date(start).toISOString(), end: new Date(end).toISOString(), services: ["synthetics"],
                 ...(bucket ? { group_by: [`ts:${bucket}`], limit: 10000 } : {}) };
  const scope: Where[] = [{ field: "attributes.check.id", op: "in", value: slo.checks }, ...notExcluded(excluded)];
  if (slo.type === "availability") {
    const q = { ...base, where: [{ field: "metric_name", op: "=", value: SUCCESS }, ...scope], aggs: [{ fn: "sum", field: "value" }, { fn: "count" }] };
    return { good: q, total: q };                                     // one query: sum (passes) and count
  }
  const runs = [{ field: "metric_name", op: "=", value: DURATION }, ...scope];
  return { total: { ...base, where: runs, aggs: [{ fn: "count" }] },
           good: { ...base, where: [...runs, { field: "value", op: "<=", value: slo.threshold_ms ?? 0 }], aggs: [{ fn: "count" }] } };
}

const goodOf = (slo: Slo, r: Result | null, row = 0) => Number(r?.rows[row]?.[slo.type === "availability" ? r.columns.indexOf("sum(value)") : r.columns.indexOf("count")] ?? 0);
const totalOf = (r: Result | null, row = 0) => Number(r?.rows[row]?.[r.columns.indexOf("count")] ?? 0);

function useSlo(slo: Slo | null, excluded: string[], tick: number) {
  const now = useMemo(() => Date.now(), [tick]);   // eslint-disable-line react-hooks/exhaustive-deps
  const key = `${slo?.id}:${slo?.updated_at ?? ""}:${excluded.length}:${tick}`;
  const q = slo ? sloQueries(slo, excluded, now - slo.window_days * DAY, now) : null;
  const h = slo ? sloQueries(slo, excluded, now - HOUR, now) : null;
  const good = useQuery(q?.good ?? null, "g" + key), total = useQuery(q && q.total !== q.good ? q.total : null, "t" + key);
  const good1h = useQuery(h?.good ?? null, "h" + key), total1h = useQuery(h && h.total !== h.good ? h.total : null, "i" + key);
  const perf = slo?.type === "performance";
  const loaded: Loaded = { data: good.data && (!perf || total.data) ? good.data : null, error: good.error ?? total.error, loading: good.loading || total.loading };
  const status = slo && loaded.data
    ? evaluate(slo.target, goodOf(slo, good.data), totalOf(perf ? total.data : good.data),
               goodOf(slo, good1h.data), totalOf(perf ? total1h.data : good1h.data))
    : null;
  return { status, loaded, now };
}

const pct = (v: number | null, digits = 3) => (v == null ? "—" : `${Number(v.toFixed(digits))}%`);
const stateColor = (s: SloStatus["state"]) => (s === "healthy" ? OK : s === "at risk" ? WARN : s === "breached" ? BAD : "var(--text-3)");
const budgetText = (b: number | null) => (b == null ? "—" : b === -Infinity ? "used up" : `${Math.round(Math.max(-9.99, b) * 100)}%`);

function BudgetBar({ left }: { left: number | null }) {
  const v = left == null || left === -Infinity ? 0 : Math.max(0, Math.min(1, left));
  const color = left == null ? "var(--text-3)" : v >= 0.25 ? OK : v > 0 ? WARN : BAD;
  return <div className="budget-bar" title={`error budget left: ${budgetText(left)}`}><i style={{ width: `${v * 100}%`, background: color }} /></div>;
}

// ------------------------------------------------------------------ list

function SloList({ ctx }: { ctx: Ctx }) {
  const [items, setItems] = useState<Slo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const all = useChecks(ctx.tick);
  useEffect(() => { slos.list().then((d) => setItems(d.items), (e: Error) => setError(e.message)); }, [ctx.tick]);
  return (
    <>
      <div className="toolbar">
        <span className="faint">An SLO is a promise such as “checkout works 99.9% of the time over 30 days”. Each one counts its checks' runs, leaving out excluded runs.</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn primary" disabled={!all?.length} title={all?.length ? "" : "Create a synthetic check first"} onClick={() => ctx.go("/slos/new")}>New SLO</button>
      </div>
      <Panel title="Service level objectives" flush>
        {error ? <div className="state error">{error}</div>
          : !items || !all ? <div className="skeleton" style={{ height: 160 }} />
          : !items.length ? (
            <div className="state" style={{ minHeight: 180, flexDirection: "column", gap: 10 }}>
              <div>No SLOs yet. Pick one or more synthetic checks and a target, and Leasyd tracks how much error budget is left.</div>
              {all.length ? <button className="btn primary" onClick={() => ctx.go("/slos/new")}>Create your first SLO</button>
                          : <button className="btn" onClick={() => ctx.go("/synthetics/new")}>Create a synthetic check first</button>}
            </div>
          ) : (
            <table className="slo-table">
              <thead><tr><th>SLO</th><th>target</th><th>current</th><th>error budget left</th><th>status</th></tr></thead>
              <tbody>{items.map((s) => <SloRow key={s.id} slo={s} checks={all} ctx={ctx} />)}</tbody>
            </table>
          )}
      </Panel>
    </>
  );
}

function SloRow({ slo, checks: all, ctx }: { slo: Slo; checks: Check[]; ctx: Ctx }) {
  const excluded = all.filter((c) => slo.checks.includes(c.id)).flatMap((c) => c.excluded_runs ?? []);
  const { status, loaded } = useSlo(slo, excluded, ctx.tick);
  return (
    <tr onClick={() => ctx.go(`/slos/${slo.id}`)}>
      <td><span className="link">{slo.name} ›</span><div className="faint">{describe(slo, all)}</div></td>
      <td className="mono">{slo.target}% · {slo.window_days} d</td>
      <td className="mono">{loaded.error ? <span className="faint">error</span> : status ? pct(status.attainment) : "…"}</td>
      <td><BudgetBar left={status?.budgetLeft ?? null} /> <span className="mono faint">{status ? budgetText(status.budgetLeft) : ""}</span></td>
      <td>{status && <span className="slo-state" style={{ color: stateColor(status.state), borderColor: stateColor(status.state) }}>{status.state}</span>}</td>
    </tr>
  );
}

function describe(slo: Slo, all: Check[]) {
  const names = slo.checks.map((id) => all.find((c) => c.id === id)?.name ?? "(deleted check)").join(", ");
  return slo.type === "availability" ? `Runs of ${names} that pass` : `Runs of ${names} within ${fmtNum(slo.threshold_ms ?? 0)} ms`;
}

// ------------------------------------------------------------------ one SLO

function SloDetail({ ctx, id }: { ctx: Ctx; id: string }) {
  const [slo, setSlo] = useState<Slo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const all = useChecks(ctx.tick);
  useEffect(() => { slos.get(id).then(setSlo, (e: Error) => setError(e.message)); }, [id, ctx.tick]);
  const excluded = (all ?? []).filter((c) => slo?.checks.includes(c.id)).flatMap((c) => c.excluded_runs ?? []);
  const { status, loaded, now } = useSlo(slo, excluded, ctx.tick);
  const daily = slo ? sloQueries(slo, excluded, now - slo.window_days * DAY, now, 86400) : null;
  const key = `${slo?.id}:${slo?.updated_at ?? ""}:${excluded.length}:${ctx.tick}`;   // changes once the SLO has loaded
  const dGood = useQuery(daily?.good ?? null, "dg" + key), dTotal = useQuery(daily && daily.total !== daily.good ? daily.total : null, "dt" + key);

  if (error) return <div className="state error">{error}</div>;
  if (!slo || !all) return <div className="skeleton" style={{ height: 240 }} />;
  const range = customRange(now - slo.window_days * DAY, now);
  // Per day: attainment, and the budget left at the end of each day (cumulative over the window).
  const perf = slo.type === "performance";
  const days = new Map<number, { good: number; total: number }>();
  for (const [i, row] of (dGood.data?.rows ?? []).entries()) {
    const t = Date.parse(String(row[0]));
    days.set(t, { good: goodOf(slo, dGood.data, i), total: perf ? 0 : totalOf(dGood.data, i) });
  }
  if (perf) for (const [i, row] of (dTotal.data?.rows ?? []).entries()) {
    const t = Date.parse(String(row[0]));
    days.set(t, { good: days.get(t)?.good ?? 0, total: totalOf(dTotal.data, i) });
  }
  const sorted = [...days.entries()].sort((a, b) => a[0] - b[0]);
  let g = 0, n = 0;
  const attainPts: [number, number][] = [], budgetPts: [number, number][] = [];
  for (const [t, d] of sorted) {
    if (d.total) attainPts.push([t, (d.good / d.total) * 100]);
    g += d.good; n += d.total;
    const b = evaluate(slo.target, g, n).budgetLeft;
    if (b != null) budgetPts.push([t, Math.max(b, 0) * 100]);          // 0% = used up
  }
  const minutes = slo.window_days * 1440 * (100 - slo.target) / 100;
  const remove = async () => {
    if (!confirm(`Delete the SLO “${slo.name}”? The checks and their results stay.`)) return;
    try { await slos.remove(id); ctx.go("/slos"); } catch (e) { setError((e as Error).message); }
  };
  return (
    <>
      <div className="toolbar">
        <a href="#/slos" className="btn" onClick={(e) => { e.preventDefault(); ctx.go("/slos"); }}>← All SLOs</a>
        <span className="faint">Always the last {slo.window_days} days, whatever the time range above.</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn" onClick={() => ctx.go(`/alerts/rules/new?slo=${id}`)}>Alert me</button>
        <button className="btn" onClick={() => ctx.go(`/slos/${id}/edit`)}>Edit</button>
        <button className="btn" onClick={remove}>Delete</button>
      </div>
      <Panel title={slo.name} right={status && <span className="slo-state" style={{ color: stateColor(status.state), borderColor: stateColor(status.state) }}>{status.state}</span>}>
        <div className="faint" style={{ marginBottom: 10 }}>{slo.description ? `${slo.description} · ` : ""}{describe(slo, all)} · target {slo.target}% over {slo.window_days} days</div>
        <div className="grid">
          <div className="span-3"><Loads q={loaded} height={84}>{() => <Stat small value={pct(status?.attainment ?? null)} sub={`current (target ${slo.target}%)`} />}</Loads></div>
          <div className="span-3"><Loads q={loaded} height={84}>{() => <Stat small value={budgetText(status?.budgetLeft ?? null)} sub="error budget left" />}</Loads></div>
          <div className="span-3"><Loads q={loaded} height={84}>{() => <Stat small value={status ? `${fmtNum(status.bad)} / ${fmtNum(Math.floor(status.allowedBad))}` : "—"}
                                                                                 sub={`bad runs / allowed (of ${fmtNum(status?.total ?? 0)})`} />}</Loads></div>
          <div className="span-3"><Loads q={loaded} height={84}>{() => <Stat small value={status?.burn1h == null ? "—" : `${status.burn1h.toFixed(1)}×`}
                                                                                 sub="burn rate, last hour (1× = on budget)" />}</Loads></div>
        </div>
        <div className="faint" style={{ marginTop: 8 }}>
          The budget is {(100 - slo.target).toFixed(3).replace(/\.?0+$/, "")}% of runs, about {fmtNum(Math.round(minutes))} minutes of failure over {slo.window_days} days.
          {excluded.length ? ` ${excluded.length} excluded run${excluded.length > 1 ? "s are" : " is"} left out, as are runs during maintenance windows.` : " Runs during maintenance windows are left out."}
        </div>
      </Panel>
      <div className="grid">
        <Panel title="Each day" span={6}>
          <Loads q={dGood} empty={!attainPts.length} height={200}>
            {() => <TimeSeries series={[{ label: "% good", color: "var(--series-1)", points: attainPts },
                                        { label: "target", color: WARN, points: attainPts.map(([t]) => [t, slo.target]) }]}
                               range={range} unit="%" height={200} area={false} />}
          </Loads>
        </Panel>
        <Panel title="Error budget left" span={6}>
          <Loads q={dGood} empty={!budgetPts.length} height={200}>
            {() => <TimeSeries series={[{ label: "% of budget left", color: OK, points: budgetPts }]} range={range} unit="%" height={200} />}
          </Loads>
        </Panel>
      </div>
      <Panel title="Checks" flush>
        <RankTable head={["check", "type", "every"]} onRow={(i) => ctx.go(`/synthetics/${slo.checks[i]}`)}
                   rows={slo.checks.map((cid) => { const c = all.find((x) => x.id === cid);
                     return [<span className="link">{c?.name ?? "(deleted check)"} ›</span>, c?.type === "browser" ? "browser" : "HTTP", c ? `${c.frequency} min` : "—"]; })} />
      </Panel>
    </>
  );
}

// ------------------------------------------------------------------ create / edit

function EditSlo({ ctx, id }: { ctx: Ctx; id: string }) {
  const [slo, setSlo] = useState<Slo | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { slos.get(id).then(setSlo, (e: Error) => setError(e.message)); }, [id]);
  if (error) return <div className="state error">{error}</div>;
  return slo ? <SloForm ctx={ctx} existing={slo} /> : <div className="skeleton" style={{ height: 240 }} />;
}

function SloForm({ ctx, existing }: { ctx: Ctx; existing?: Slo }) {
  const all = useChecks(0);
  const [s, setS] = useState<Omit<Slo, "id">>(existing ?? { name: "", description: "", type: "availability", checks: [], target: 99.9, window_days: 30, threshold_ms: 1000 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (p: Partial<Slo>) => setS({ ...s, ...p });
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!s.checks.length) return setError("Choose at least one check.");
    setBusy(true); setError(null);
    const body = { ...s, ...(s.type === "availability" ? { threshold_ms: undefined } : {}) };
    try {
      const saved = existing ? await slos.update(existing.id, body) : await slos.create(body);
      ctx.go(`/slos/${saved.id}`);
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  const minutes = s.window_days * 1440 * (100 - s.target) / 100;
  return (
    <form onSubmit={submit}>
      <div className="toolbar">
        <a href="#/slos" className="btn" onClick={(e) => { e.preventDefault(); ctx.go(existing ? `/slos/${existing.id}` : "/slos"); }}>← Cancel</a>
      </div>
      <Panel title={existing ? `Edit “${existing.name}”` : "New SLO"}>
        <section className="form-section">
          <div className="form-grid">
            <label>Name<input className="input" required maxLength={80} value={s.name} onChange={(e) => set({ name: e.target.value })} placeholder="Checkout available" /></label>
            <label className="wide">Description (optional)<input className="input" maxLength={500} value={s.description} onChange={(e) => set({ description: e.target.value })}
                                                                  placeholder="Customers can complete a purchase" /></label>
          </div>
        </section>
        <section className="form-section">
          <h3>What counts as good</h3>
          <div className="type-pick" role="radiogroup">
            <button type="button" role="radio" aria-checked={s.type === "availability"} className={s.type === "availability" ? "on" : ""} onClick={() => set({ type: "availability" })}>
              <b>Availability</b><span>A run is good when it passes.</span>
            </button>
            <button type="button" role="radio" aria-checked={s.type === "performance"} className={s.type === "performance" ? "on" : ""} onClick={() => set({ type: "performance" })}>
              <b>Performance</b><span>A run is good when all its steps together take at most a set time.</span>
            </button>
          </div>
          {s.type === "performance" && (
            <div className="form-grid" style={{ marginTop: 10 }}>
              <label>At most (ms)<input className="input" type="number" min={1} max={60000} required value={s.threshold_ms ?? 1000} onChange={(e) => set({ threshold_ms: Number(e.target.value) })} /></label>
            </div>
          )}
        </section>
        <section className="form-section">
          <h3>Checks</h3>
          {!all ? <div className="skeleton" style={{ height: 40 }} /> : (
            <div className="checks">
              {all.map((c) => (
                <label key={c.id} className="check"><input type="checkbox" checked={s.checks.includes(c.id)}
                  onChange={() => set({ checks: s.checks.includes(c.id) ? s.checks.filter((x) => x !== c.id) : [...s.checks, c.id] })} /> {c.name}</label>
              ))}
            </div>
          )}
        </section>
        <section className="form-section">
          <h3>Target</h3>
          <div className="form-grid">
            <label>Good runs (%)<input className="input" type="number" min={50} max={99.999} step={0.001} required value={s.target} onChange={(e) => set({ target: Number(e.target.value) })} /></label>
            <label>Over the last
              <select className="select" value={s.window_days} onChange={(e) => set({ window_days: Number(e.target.value) })}>
                {[7, 14, 30].map((d) => <option key={d} value={d}>{d} days</option>)}
              </select>
            </label>
          </div>
          <div className="faint">Error budget: {(100 - s.target).toFixed(3).replace(/\.?0+$/, "")}% of runs, about {fmtNum(Math.round(minutes))} minutes of failure in {s.window_days} days.</div>
        </section>
        {error && <div className="form-error" role="alert" style={{ marginTop: 10 }}>{error}</div>}
        <div className="toolbar" style={{ marginTop: 14 }}>
          <button className="btn primary" disabled={busy}>{busy ? "Saving…" : existing ? "Save" : "Create SLO"}</button>
        </div>
      </Panel>
    </form>
  );
}
