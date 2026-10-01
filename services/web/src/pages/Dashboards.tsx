// Dashboards: a list on the left (search, favorites, create), the chosen dashboard on the right:
// its filters (e.g. Service Name), and a grid of panels, each one or more PromQL queries.
import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { checks, Dashboard, dashboards, DashboardSummary, Panel, PanelType, promql, promqlAt, PromSeries, records } from "../api";
import type { Ctx } from "../App";
import { BUILTIN } from "../builtinDashboards";
import { Drawer, StackedBars } from "../components/Charts";
import { Series, TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtUnit, Range, rangeWindow } from "../time";
import { useQuery } from "../useQuery";

const PALETTE = ["#b49bf3", "#6ea8fe", "#d7b98a", "#6fd3c1", "#f59ab4", "#9aa6b8", "#7fb77e", "#e07a5f", "#8d99ae", "#f2cc8f"];
const ROW_PX = 170;
const UNITS: [string, string][] = [["", "Number"], ["/s", "Per second"], ["%", "Percent (0-100)"], ["percentunit", "Percent (0-1)"],
  ["ms", "Milliseconds"], ["s", "Seconds"], ["ns", "Nanoseconds"], ["bytes", "Bytes"]];
const TYPES: [PanelType, string][] = [["timeseries", "Line"], ["bars", "Stacked bars"], ["stat", "Number"], ["text", "Text"]];

function favorites(): string[] { try { return JSON.parse(localStorage.getItem("leasyd.dash.fav") ?? "[]"); } catch { return []; } }
function saveFavorites(f: string[]) { try { localStorage.setItem("leasyd.dash.fav", JSON.stringify(f)); } catch { /* ignore */ } }

export function Dashboards({ ctx, path }: { ctx: Ctx; path: string }) {
  const [, , id, mode] = path.split("/");          // /dashboards[/<id>[/edit]]
  const [list, setList] = useState<DashboardSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [fav, setFav] = useState<string[]>(favorites);
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  useEffect(() => { dashboards.list().then((r) => setList(r.items), (e: Error) => setError(e.message)); }, [ctx.tick, nonce]);

  const all: (DashboardSummary & { builtin?: boolean })[] = [
    ...BUILTIN.map((d) => ({ id: d.id, name: d.name, description: d.description, panels: d.panels.length, version: 1, builtin: true })),
    ...(list ?? []),
  ];
  const shown = all.filter((d) => `${d.name} ${d.description}`.toLowerCase().includes(search.trim().toLowerCase()));
  const current = id ?? fav.find((f) => all.some((d) => d.id === f)) ?? all[0]?.id;
  const toggleFav = (d: string) => { const f = fav.includes(d) ? fav.filter((x) => x !== d) : [...fav, d]; setFav(f); saveFavorites(f); };
  const create = async () => {
    try {
      const d = await dashboards.create({ name: "New dashboard", description: "", variables: [{ name: "service_name", label: "Service Name" }], panels: [] });
      reload(); ctx.go(`/dashboards/${d.id}/edit`);
    } catch (e) { setError((e as Error).message); }
  };
  const item = (d: (typeof all)[number]) => (
    <div key={d.id} className={`dash-item${d.id === current ? " on" : ""}`} onClick={() => ctx.go(`/dashboards/${d.id}`)}>
      <div className="dash-item-head">
        <span className="dash-item-name" title={d.name}>{d.name}</span>
        {d.builtin ? <span className="pill">Built-in</span> : <span className="pill">Shared</span>}
        <button type="button" className={`star${fav.includes(d.id) ? " on" : ""}`} title={fav.includes(d.id) ? "Remove from favorites" : "Add to favorites"}
                onClick={(e) => { e.stopPropagation(); toggleFav(d.id); }}>{fav.includes(d.id) ? "★" : "☆"}</button>
      </div>
      {d.description && <div className="dash-item-desc" title={d.description}>{d.description}</div>}
    </div>
  );
  const favs = shown.filter((d) => fav.includes(d.id));
  return (
    <div className="dash">
      <aside className="dash-list">
        <div className="dash-list-top">
          <input className="input grow" placeholder="Search…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search dashboards" />
          <button className="btn" onClick={create} title="New dashboard">＋ Create</button>
        </div>
        {error && <div className="form-error" style={{ padding: "0 12px" }}>{error}</div>}
        <div className="dash-list-section">Favorites</div>
        {favs.length ? favs.map(item) : <div className="faint dash-list-empty">Star a dashboard to keep it here.</div>}
        <div className="dash-list-section">All dashboards</div>
        {!list && !error ? <div className="skeleton" style={{ height: 120, margin: 12 }} /> : shown.map(item)}
      </aside>
      <div className="dash-main">
        {current ? <DashboardView key={`${current}:${nonce}`} ctx={ctx} id={current} edit={mode === "edit"} onChanged={reload} /> :
          <div className="state">No dashboards yet</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ one dashboard

function DashboardView({ ctx, id, edit, onChanged }: { ctx: Ctx; id: string; edit: boolean; onChanged: () => void }) {
  const [saved, setSaved] = useState<Dashboard | null>(null);
  const [draft, setDraft] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Panel | null>(null);
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState(false);
  const [vars, setVars] = useState<Record<string, string[]>>({});
  useEffect(() => {
    setError(null);
    const builtin = BUILTIN.find((d) => d.id === id);
    if (builtin) { setSaved(builtin); setDraft(builtin); return; }
    dashboards.get(id).then((d) => { setSaved(d); setDraft(d); }, (e: Error) => setError(e.message));
  }, [id]);
  if (error && !saved) return <div className="state error">{error}</div>;
  if (!saved || !draft) return <div className="skeleton" style={{ height: 300 }} />;
  const d = edit ? draft : saved;
  const readOnly = !!saved.builtin;
  const set = (p: Partial<Dashboard>) => setDraft({ ...draft, ...p });
  const setPanels = (panels: Panel[]) => set({ panels });
  const save = async () => {
    setBusy(true); setError(null);
    try {
      const r = await dashboards.update(id, { name: draft.name, description: draft.description, variables: draft.variables, panels: draft.panels, version: saved.version });
      setSaved(r); setDraft(r); onChanged(); ctx.go(`/dashboards/${id}`);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const clone = async () => {
    try {
      const r = await dashboards.create({ name: `${saved.name} (Clone)`, description: saved.description, variables: saved.variables,
                                          panels: saved.panels.map(({ id: pid, ...p }) => ({ ...p, id: pid })) });
      onChanged(); ctx.go(`/dashboards/${r.id}`);
    } catch (e) { setError((e as Error).message); }
  };
  const remove = async () => {
    if (!confirm(`Delete the dashboard “${saved.name}” for everyone?`)) return;
    try { await dashboards.remove(id); onChanged(); ctx.go("/dashboards"); } catch (e) { setError((e as Error).message); }
  };
  const move = (i: number, by: number) => {
    const p = [...draft.panels], j = Math.max(0, Math.min(p.length - 1, i + by));
    [p[i], p[j]] = [p[j], p[i]]; setPanels(p);
  };
  const resize = (i: number, dw: number, dh: number) =>
    setPanels(draft.panels.map((p, j) => (j === i ? { ...p, w: Math.max(2, Math.min(12, p.w + dw)), h: Math.max(1, Math.min(6, p.h + dh)) } : p)));
  const upsert = (p: Panel) => {
    const exists = draft.panels.some((x) => x.id === p.id);
    setPanels(exists ? draft.panels.map((x) => (x.id === p.id ? p : x)) : [...draft.panels, p]);
    setEditing(null);
  };
  const newPanel = (type: PanelType): Panel => ({ id: Math.random().toString(16).slice(2, 10), type, title: type === "text" ? "Notes" : "New panel", description: "",
    w: type === "text" ? 3 : type === "stat" ? 3 : 6, h: type === "stat" ? 1 : 2, unit: "",
    ...(type === "text" ? { text: "" } : { queries: [{ promql: 'sum by (service_name) (rate(leasyd.spans{service_name=~"$service_name"}[$__interval]))', legend: "{{service_name}}" }] }) });

  return (
    <>
      <header className="dash-head">
        <div className="dash-title">
          {edit ? <input className="input dash-name-input" value={draft.name} maxLength={100} onChange={(e) => set({ name: e.target.value })} aria-label="Dashboard name" />
            : <h1>{d.name}</h1>}
          <span className="faint">(v{saved.version}{readOnly && <span className="pill readonly">Read-only</span>})</span>
          {saved.updated_by && !edit && <span className="faint" title={saved.updated_at}>edited by {saved.updated_by}</span>}
        </div>
        <span className="spacer" />
        {edit ? (<>
          <button className="btn" onClick={() => setEditing(newPanel("timeseries"))}>＋ Panel</button>
          <button className="btn" onClick={() => setEditing(newPanel("text"))}>＋ Text</button>
          <button className="btn" onClick={() => { setDraft(saved); ctx.go(`/dashboards/${id}`); }}>Cancel</button>
          <button className="btn primary" disabled={busy || !draft.name.trim()} onClick={save}>{busy ? "Saving…" : "Save"}</button>
        </>) : (<>
          <button className="btn" onClick={clone}>Clone</button>
          {!readOnly && <button className="btn" onClick={() => ctx.go(`/dashboards/${id}/edit`)}>Edit</button>}
          {!readOnly && (
            <div className="menu">
              <button className="btn" aria-label="More" onClick={() => setMenu(!menu)}>⋮</button>
              {menu && <div className="menu-list" style={{ right: 0, left: "auto" }}><button onClick={remove}>Delete dashboard</button></div>}
            </div>)}
        </>)}
      </header>
      {edit && (
        <div className="dash-edit-bar">
          <input className="input grow" placeholder="Description" maxLength={500} value={draft.description} onChange={(e) => set({ description: e.target.value })} />
          <label className="side-check"><input type="checkbox" checked={draft.variables.some((v) => v.name === "service_name")}
            onChange={(e) => set({ variables: e.target.checked ? [{ name: "service_name", label: "Service Name" }] : draft.variables.filter((v) => v.name !== "service_name") })} />
            Service Name filter (<code>$service_name</code>)</label>
        </div>
      )}
      {!edit && d.description && <div className="faint dash-desc">{d.description}</div>}
      {error && <div className="result-box fail" role="alert">{error}</div>}
      {d.variables.length > 0 && <Variables ctx={ctx} variables={d.variables} values={vars} onChange={setVars} />}
      {!d.panels.length ? (
        <div className="state" style={{ minHeight: 220, flexDirection: "column", gap: 10 }}>
          <div>{edit ? "Add a panel: a chart of any PromQL query over your logs, spans and metrics." : "This dashboard has no panels yet."}</div>
          {edit ? <button className="btn primary" onClick={() => setEditing(newPanel("timeseries"))}>Add a panel</button>
            : !readOnly && <button className="btn primary" onClick={() => ctx.go(`/dashboards/${id}/edit`)}>Edit</button>}
        </div>
      ) : (
        <div className="dash-grid">
          {d.panels.map((p, i) => (
            <PanelCard key={p.id} ctx={ctx} panel={p} vars={vars} edit={edit}
                       tools={edit && (
                         <div className="panel-tools" onClick={(e) => e.stopPropagation()}>
                           <button title="Earlier" onClick={() => move(i, -1)}>◀</button>
                           <button title="Later" onClick={() => move(i, 1)}>▶</button>
                           <button title="Narrower" onClick={() => resize(i, -1, 0)}>−W</button>
                           <button title="Wider" onClick={() => resize(i, 1, 0)}>+W</button>
                           <button title="Shorter" onClick={() => resize(i, 0, -1)}>−H</button>
                           <button title="Taller" onClick={() => resize(i, 0, 1)}>+H</button>
                           <button title="Edit" onClick={() => setEditing(p)}>✎</button>
                           <button title="Remove" onClick={() => setPanels(draft.panels.filter((x) => x.id !== p.id))}>✕</button>
                         </div>
                       )} />
          ))}
        </div>
      )}
      {editing && <PanelEditor ctx={ctx} panel={editing} vars={vars} onCancel={() => setEditing(null)} onSave={upsert} />}
    </>
  );
}

// ------------------------------------------------------------------ filters ($service_name)

function Variables({ ctx, variables, values, onChange }: { ctx: Ctx; variables: { name: string; label: string }[];
                    values: Record<string, string[]>; onChange: (v: Record<string, string[]>) => void }) {
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const opts = { group_by: ["service"], aggs: [{ fn: "count" }], limit: 500, ...w };
  const t = useQuery({ signal: "traces", ...opts }, "vt" + ctx.range.key + ctx.tick);
  const l = useQuery({ signal: "logs", ...opts }, "vl" + ctx.range.key + ctx.tick);
  const m = useQuery({ signal: "metrics", ...opts }, "vm" + ctx.range.key + ctx.tick);
  const services = [...new Set([t, l, m].flatMap((q) => (q.data ? records(q.data).map((r) => String(r.service)) : [])))].sort();
  return (
    <div className="dash-vars">
      {variables.map((v) => (
        <VariablePicker key={v.name} label={v.label} options={v.name === "service_name" ? services : []} selected={values[v.name] ?? []}
                        onChange={(s) => onChange({ ...values, [v.name]: s })} />
      ))}
    </div>
  );
}

function VariablePicker({ label, options, selected, onChange }: { label: string; options: string[]; selected: string[]; onChange: (s: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    addEventListener("mousedown", close);
    return () => removeEventListener("mousedown", close);
  }, []);
  return (
    <div className="menu" ref={ref}>
      <button type="button" className="var-chip" onClick={() => setOpen(!open)}>
        <span>{label}</span><b>{selected.length ? selected.join(", ") : "is any"}</b>
      </button>
      {open && (
        <div className="menu-list columns-menu" style={{ maxHeight: 320, overflow: "auto" }}>
          <label><input type="checkbox" checked={!selected.length} onChange={() => onChange([])} />is any</label>
          {options.map((o) => (
            <label key={o}><input type="checkbox" checked={selected.includes(o)}
                                  onChange={(e) => onChange(e.target.checked ? [...selected, o] : selected.filter((x) => x !== o))} />{o}</label>
          ))}
          {!options.length && <div className="faint" style={{ padding: 6 }}>No values in this time range</div>}
        </div>
      )}
    </div>
  );
}

const reEscape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** A panel query with the dashboard's filters and the step filled in. */
/** $__interval: the chart's step; $__range: the whole time range (number panels evaluate once, at its end). */
export function fillQuery(text: string, vars: Record<string, string[]>, step: number, rangeS = step): string {
  let out = text.replace(/\$__rate_interval|\$__interval/g, `${step}s`).replace(/\$__range/g, `${rangeS}s`);
  for (const [name, vals] of Object.entries(vars)) {
    const re = vals.length ? vals.map(reEscape).join("|") : ".*";
    out = out.replace(new RegExp(`\\$\\{?${name}\\}?`, "g"), re.replace(/\\/g, "\\\\").replace(/"/g, '\\"'));
  }
  return out.replace(/\$\{?service_name\}?/g, ".*");
}

// ------------------------------------------------------------------ panels

/** At most 6 panel queries in flight; the rest wait their turn. */
const queue: (() => void)[] = [];
let running = 0;
function limited<T>(f: () => Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const go = () => { running++; f().then(resolve, reject).finally(() => { running--; queue.shift()?.(); }); };
    if (running < 6) go(); else queue.push(go);
  });
}

type Loaded = { series: (Series & { last: number })[]; error?: string } | null;

function usePanelData(panel: Panel, vars: Record<string, string[]>, range: Range, tick: number): Loaded {
  const [state, setState] = useState<Loaded>(null);
  const step = bucketSeconds(range);
  const key = JSON.stringify([panel.queries, vars, range.key, range.from, range.to, tick]);
  useEffect(() => {
    if (panel.type === "text" || !panel.queries?.length) return;
    let live = true;
    setState(null);
    const w = rangeWindow(range);
    // Number panels: one value over the page's whole range, at its end (minute-aligned).
    const at = Math.floor(Date.parse(w.end) / 60_000) * 60, rangeS = Math.max(60, Math.round((Date.parse(w.end) - Date.parse(w.start)) / 60_000) * 60);
    const run = (q: { promql: string }) => panel.type === "stat"
      ? promqlAt({ promql: fillQuery(q.promql, vars, step, rangeS), time: at })
          .then((r) => r.data.result.map((x) => ({ metric: x.metric, values: [x.value] as [number, string][] })))
      : promql({ promql: fillQuery(q.promql, vars, step, rangeS), start: w.start, end: w.end, step }).then((r) => r.data.result);
    const names = panel.queries.some((q) => q.promql.includes("check_id")) ? checkNames() : Promise.resolve(null);
    Promise.all([names, ...panel.queries.map((q) => limited(() => run(q).then((result) => ({ q, result }))))])
      .then(([nm, ...all]) => {
        const nameMap = nm as Map<string, string> | null;
        if (!live) return;
        const series: (Series & { last: number })[] = [];
        (all as { q: { promql: string; legend?: string }; result: PromSeries[] }[]).forEach(({ q, result }, qi) => result.forEach((s0: PromSeries) => {
          const s = { ...s0, metric: withCheckName(s0.metric, nameMap) };
          const pts = s.values.map(([t, v]) => [t * 1000, Number(v)] as [number, number]).filter((p) => isFinite(p[1]));
          if (!pts.length) return;
          series.push({ label: legend(q.legend, s.metric, qi, panel.queries!.length), color: PALETTE[series.length % PALETTE.length], points: pts, last: pts[pts.length - 1][1] });
        }));
        setState({ series });
      }, (e: Error) => live && setState({ series: [], error: e.message }));
    return () => { live = false; };
  }, [key]);   // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

/** Synthetic checks' current names by id: series grouped by check_id show the name a check has now
 *  (a renamed check stays one series); a deleted check says so. Loaded once per page load. */
let checkNamesP: Promise<Map<string, string>> | null = null;
function checkNames(): Promise<Map<string, string>> {
  if (!checkNamesP) checkNamesP = checks.list().then((r) => new Map(r.checks.map((c) => [c.id, c.name] as [string, string])), () => new Map<string, string>());
  return checkNamesP;
}
function withCheckName(metric: Record<string, string>, names: Map<string, string> | null): Record<string, string> {
  if (!names || !metric.check_id || metric.check_name) return metric;
  return { ...metric, check_name: names.get(metric.check_id) ?? `deleted check (${metric.check_id})` };
}

function legend(template: string | undefined, metric: Record<string, string>, qi: number, nq: number): string {
  if (template) return template.replace(/\{\{\s*([^}\s]+)\s*\}\}/g, (_, k) => metric[k] ?? metric[k.replace(/_/g, ".")] ?? "");
  const labels = Object.entries(metric).filter(([k]) => k !== "__name__").map(([, v]) => v);
  return labels.length ? labels.join(" · ") : nq > 1 ? `Query ${qi + 1}` : metric.__name__ ?? "value";
}

function PanelCard({ ctx, panel, vars, edit, tools }: { ctx: Ctx; panel: Panel; vars: Record<string, string[]>; edit: boolean; tools?: ReactNode }) {
  const data = usePanelData(panel, vars, ctx.range, ctx.tick);
  const step = bucketSeconds(ctx.range);
  const fmt = (v: number) => fmtUnit(v, panel.unit ?? "", panel.decimals);
  const bodyH = panel.h * ROW_PX + (panel.h - 1) * 12 - 40;          // the grid row heights and gaps, less the header
  const chartH = Math.max(70, bodyH - 16 - 26);                         // less the padding and the legend
  const open = () => panel.queries?.[0] && ctx.go(`/query?promql=${encodeURIComponent(fillQuery(panel.queries[0].promql, vars, 300).replace(`${300}s`, "5m"))}`);
  return (
    <section className={`dash-panel${panel.type === "text" ? " text" : ""}${edit ? " editing" : ""}`}
             style={{ gridColumn: `span ${panel.w}`, gridRow: `span ${panel.h}` }}>
      <header className="dash-panel-head" title={panel.description || panel.title}>
        <b>{panel.title}</b>{panel.description && <span className="faint">{panel.description}</span>}
        <span className="spacer" />
        {!edit && panel.type !== "text" && <button type="button" className="dash-panel-open" title="Open in Query Builder" onClick={open}>↗</button>}
        {tools}
      </header>
      <div className="dash-panel-body" style={{ height: bodyH }}>
        {panel.type === "text" ? <TextBody text={panel.text ?? ""} />
          : !data ? <div className="skeleton" style={{ height: "100%" }} />
          : data.error ? <div className="state error" style={{ minHeight: 0, height: "100%" }} title={data.error}>{data.error.slice(0, 160)}</div>
          : !data.series.length ? <div className="state" style={{ minHeight: 0, height: "100%" }}>No data</div>
          : panel.type === "stat" ? <StatBody series={data.series} fmt={fmt} />
          : panel.type === "bars" ? (
            <StackedBars bars={barsOf(data.series)} keys={data.series.map((s) => ({ label: s.label, color: s.color }))} range={ctx.range}
                         bucketMs={step * 1000} height={chartH} format={fmt} legend={false} />
          ) : <TimeSeries series={data.series} range={ctx.range} height={chartH} format={fmt} area={false} legend={false} dots />}
        {data && data.series.length > 0 && panel.type !== "stat" && panel.type !== "text" && (
          <div className="dash-legend">{data.series.slice(0, 12).map((s) => <span key={s.label}><i style={{ background: s.color }} />{s.label}</span>)}
            {data.series.length > 12 && <span className="faint">+{data.series.length - 12}</span>}</div>
        )}
      </div>
    </section>
  );
}

function StatBody({ series, fmt }: { series: (Series & { last: number })[]; fmt: (v: number) => string }) {
  if (series.length === 1) return <div className="stat-big">{fmt(series[0].last)}</div>;
  return (
    <div className="stat-list">
      {series.slice(0, 6).map((s) => <div key={s.label}><span className="faint">{s.label}</span><b>{fmt(s.last)}</b></div>)}
    </div>
  );
}

/** Plain text with paragraphs, **bold** and `code`. */
function TextBody({ text }: { text: string }) {
  return (
    <div className="dash-text">
      {text.split(/\n{2,}/).map((para, i) => (
        <p key={i}>{para.split(/(\*\*[^*]+\*\*|`[^`]+`)/).map((part, j) =>
          part.startsWith("**") ? <b key={j}>{part.slice(2, -2)}</b> : part.startsWith("`") ? <code key={j}>{part.slice(1, -1)}</code> : part)}</p>
      ))}
    </div>
  );
}

function barsOf(series: Series[]) {
  const times = [...new Set(series.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
  const at = series.map((s) => new Map(s.points));
  return times.map((t) => ({ t, values: at.map((m) => m.get(t) ?? 0) }));
}

// ------------------------------------------------------------------ panel editor

function PanelEditor({ ctx, panel, vars, onCancel, onSave }: { ctx: Ctx; panel: Panel; vars: Record<string, string[]>;
                     onCancel: () => void; onSave: (p: Panel) => void }) {
  const [p, setP] = useState<Panel>(panel);
  const [preview, setPreview] = useState<Panel>(panel);
  const set = (x: Partial<Panel>) => setP({ ...p, ...x });
  const queries = p.queries ?? [];
  const setQuery = (i: number, x: Partial<{ promql: string; legend: string }>) => set({ queries: queries.map((q, j) => (j === i ? { ...q, ...x } : q)) });
  return (
    <Drawer title={panel.title ? `Panel: ${panel.title}` : "New panel"} onClose={onCancel}
            right={<button className="btn primary" disabled={!p.title.trim() || (p.type !== "text" && !queries.some((q) => q.promql.trim()))}
                           onClick={() => onSave({ ...p, queries: p.type === "text" ? undefined : queries.filter((q) => q.promql.trim()) })}>Apply</button>}>
      <div className="drawer-section form-grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <label className="wide">Title<input className="input" maxLength={120} value={p.title} onChange={(e) => set({ title: e.target.value })} /></label>
        <label className="wide">Description<input className="input" maxLength={500} value={p.description ?? ""} onChange={(e) => set({ description: e.target.value })} /></label>
        <label>Type
          <select className="select" value={p.type} onChange={(e) => set({ type: e.target.value as PanelType, ...(e.target.value === "text" ? {} : { queries: queries.length ? queries : [{ promql: "", legend: "" }] }) })}>
            {TYPES.map(([t, l]) => <option key={t} value={t}>{l}</option>)}
          </select></label>
        {p.type !== "text" && <label>Unit
          <select className="select" value={p.unit ?? ""} onChange={(e) => set({ unit: e.target.value })}>{UNITS.map(([u, l]) => <option key={u} value={u}>{l}</option>)}</select></label>}
      </div>
      {p.type === "text" ? (
        <div className="drawer-section">
          <h4>Text</h4>
          <textarea className="input qb-editor" rows={8} maxLength={5000} value={p.text ?? ""} onChange={(e) => set({ text: e.target.value })}
                    placeholder="Explain what this part of the dashboard shows. **bold**, `code`; blank lines for paragraphs." />
        </div>
      ) : (
        <div className="drawer-section">
          <h4>Queries (PromQL)</h4>
          {queries.map((q, i) => (
            <div key={i} style={{ marginBottom: 10 }}>
              <textarea className="input mono qb-editor" rows={3} spellCheck={false} value={q.promql} onChange={(e) => setQuery(i, { promql: e.target.value })}
                        placeholder='sum by (service_name) (rate(leasyd.spans{service_name=~"$service_name"}[$__interval]))' />
              <div className="qb-filter" style={{ marginTop: 4 }}>
                <input className="input mono grow" placeholder="Legend, e.g. {{service_name}}" value={q.legend ?? ""} onChange={(e) => setQuery(i, { legend: e.target.value })} />
                {queries.length > 1 && <button className="btn" onClick={() => set({ queries: queries.filter((_, j) => j !== i) })}>✕</button>}
              </div>
            </div>
          ))}
          <div className="qb-editor-bar">
            {queries.length < 5 && <button className="linkbtn" onClick={() => set({ queries: [...queries, { promql: "", legend: "" }] })}>+ Add query</button>}
            <span className="faint"><code>$service_name</code> is the dashboard's service filter; <code>$__interval</code> the chart's step; <code>$__range</code> the page's time range (number panels show one value over it).</span>
            <span className="spacer" style={{ flex: 1 }} />
            <button className="btn" onClick={() => setPreview({ ...p, queries: queries.filter((q) => q.promql.trim()) })}>Preview</button>
          </div>
        </div>
      )}
      <div className="drawer-section">
        <h4>Preview</h4>
        <div className="dash-grid" style={{ gridTemplateColumns: "1fr" }}>
          <PanelCard ctx={ctx} panel={{ ...preview, title: p.title, description: p.description, unit: p.unit, type: p.type, text: p.text, w: 1, h: 2 }} vars={vars} edit={false} />
        </div>
      </div>
    </Drawer>
  );
}
