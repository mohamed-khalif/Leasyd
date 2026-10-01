// Query Builder: one chart from one or more queries. Each query is built on Tracing, Logging or
// Metrics (which write PromQL for you) or written as PromQL, and runs over all signals.
import { KeyboardEvent, ReactNode, useEffect, useMemo, useState } from "react";
import { dashboards, DashboardSummary, Panel, promql, PromSeries, records } from "../api";
import type { Ctx } from "../App";
import { Drawer, StackedBars } from "../components/Charts";
import { Series, TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";

type Mode = "tracing" | "logging" | "metrics" | "promql";
type Filter = { label: string; op: "=" | "!=" | "=~" | "!~"; value: string };
type Builder = { measure: string; filters: Filter[]; groupBy: string[]; metric: string; agg: string };
type Q = { id: number; name: string; mode: Mode; promql: string; b: Builder; hidden?: boolean };
type Settings = { chart: "line" | "area" | "bars"; unit: string; decimals: string; legend: boolean; threshold: string; sort: "name" | "value" };

const PALETTE = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)",
  "#7fb77e", "#e07a5f", "#8d99ae", "#f2cc8f"];
const EMPTY_B: Builder = { measure: "rate", filters: [], groupBy: ["service_name"], metric: "", agg: "sum" };
const newQuery = (id: number, mode: Mode = "tracing"): Q => ({ id, name: "", mode, promql: "", b: { ...EMPTY_B, filters: [], groupBy: ["service_name"] } });
const DEFAULT_SETTINGS: Settings = { chart: "line", unit: "", decimals: "", legend: true, threshold: "", sort: "value" };

const TRACING_MEASURES: [string, string, string][] = [
  ["rate", "Request rate", "/s"], ["errors", "Error rate", "/s"], ["error_pct", "Error percentage", "%"],
  ["p50", "Duration p50", " ms"], ["p90", "Duration p90", " ms"], ["p95", "Duration p95", " ms"], ["p99", "Duration p99", " ms"],
];
const LOGGING_MEASURES: [string, string, string][] = [["rate", "Log rate", "/s"], ["count", "Log count", ""]];
const TRACING_LABELS = ["service_name", "span_name", "span_kind", "status_code", "http.route", "http.request.method", "db.system", "rpc.method"];
const LOGGING_LABELS = ["service_name", "severity_text", "severity_range", "http.route", "k8s.namespace.name", "host.name"];
const METRIC_LABELS = ["service_name", "http.route", "http.request.method", "http.response.status_code", "host.name", "k8s.pod.name"];
const EXAMPLES: [string, string][] = [
  ["Requests per second by service", 'sum by (service_name) (rate(leasyd.spans{span_kind="SERVER"}[$__interval]))'],
  ["Error % by service", '100 * sum by (service_name) (rate(leasyd.spans{status_code="ERROR"}[$__interval])) / sum by (service_name) (rate(leasyd.spans[$__interval]))'],
  ["p95 latency (ms) by service", 'histogram_quantile(0.95, sum by (service_name) (rate(leasyd.span.duration[$__interval]))) * 1000'],
  ["Error logs per second", 'sum by (service_name) (rate(leasyd.logs{severity_range="ERROR_FATAL"}[$__interval]))'],
  ["Top 5 slowest endpoints (p99, ms)", 'topk(5, histogram_quantile(0.99, sum by (span_name) (rate(leasyd.span.duration{span_kind="SERVER"}[$__interval]))) * 1000)'],
  ["Synthetic check uptime %", '100 * avg by (check_name) (avg_over_time({"synthetics.check.success", "check.name"=~".+"}[$__interval]))'],
];

const quoteLabel = (l: string) => (/^[A-Za-z_][A-Za-z0-9_]*$/.test(l) ? l : JSON.stringify(l));
const selector = (name: string, filters: Filter[], extra: string[] = []) => {
  const m = [...filters.filter((f) => f.label && f.value !== undefined).map((f) => `${quoteLabel(f.label)}${f.op}${JSON.stringify(f.value)}`), ...extra];
  const bare = /^[A-Za-z_:][A-Za-z0-9_:.]*$/.test(name);
  if (bare) return `${name}${m.length ? `{${m.join(", ")}}` : ""}`;
  return `{${[JSON.stringify(name), ...m].join(", ")}}`;
};
const by = (g: string[]) => (g.length ? `by (${g.map(quoteLabel).join(", ")}) ` : "");

/** The PromQL a builder query stands for. */
export function toPromQL(q: Q, metricKind?: string): string {
  const b = q.b, w = "[$__interval]";
  if (q.mode === "promql") return q.promql;
  if (q.mode === "tracing") {
    const spans = (extra: string[] = []) => selector("leasyd.spans", b.filters, extra);
    if (b.measure === "rate") return `sum ${by(b.groupBy)}(rate(${spans()}${w}))`;
    if (b.measure === "errors") return `sum ${by(b.groupBy)}(rate(${spans(['status_code="ERROR"'])}${w}))`;
    if (b.measure === "error_pct")
      return `100 * sum ${by(b.groupBy)}(rate(${spans(['status_code="ERROR"'])}${w})) / sum ${by(b.groupBy)}(rate(${spans()}${w}))`;
    const phi = Number(b.measure.slice(1)) / 100;
    return `histogram_quantile(${phi}, sum ${by(b.groupBy)}(rate(${selector("leasyd.span.duration", b.filters)}${w}))) * 1000`;
  }
  if (q.mode === "logging") {
    return `sum ${by(b.groupBy)}(${b.measure === "count" ? "increase" : "rate"}(${selector("leasyd.logs", b.filters)}${w}))`;
  }
  if (!b.metric) return "";
  if (metricKind === "counter") return `${b.agg} ${by(b.groupBy)}(rate(${selector(b.metric, b.filters)}${w}))`;
  if (metricKind === "histogram" && /^p\d+$/.test(b.agg))
    return `histogram_quantile(${Number(b.agg.slice(1)) / 100}, sum by (${["le", ...b.groupBy].map(quoteLabel).join(", ")}) (rate(${selector(b.metric + "_bucket", b.filters)}${w})))`;
  if (metricKind === "histogram")
    return `sum ${by(b.groupBy)}(rate(${selector(b.metric + "_sum", b.filters)}${w})) / sum ${by(b.groupBy)}(rate(${selector(b.metric + "_count", b.filters)}${w}))`;
  return `${b.agg} ${by(b.groupBy)}(avg_over_time(${selector(b.metric, b.filters)}${w}))`;
}

const unitOf = (q: Q) => (q.mode === "tracing" ? TRACING_MEASURES.find((m) => m[0] === q.b.measure)?.[2]
  : q.mode === "logging" ? LOGGING_MEASURES.find((m) => m[0] === q.b.measure)?.[2] : undefined);

function load(params: URLSearchParams): { queries: Q[]; settings: Settings } {
  try {
    const raw = params.get("q");
    if (raw) {
      const s = JSON.parse(decodeURIComponent(escape(atob(raw))));
      if (Array.isArray(s.queries) && s.queries.length) return { queries: s.queries, settings: { ...DEFAULT_SETTINGS, ...s.settings } };
    }
  } catch { /* a bad link: start fresh */ }
  const promqlParam = params.get("promql");
  if (promqlParam) return { queries: [{ ...newQuery(1, "promql"), promql: promqlParam }], settings: DEFAULT_SETTINGS };
  return { queries: [newQuery(1)], settings: DEFAULT_SETTINGS };
}

export function QueryBuilder({ ctx, params }: { ctx: Ctx; params: URLSearchParams }) {
  const init = useMemo(() => load(params), []);   // eslint-disable-line react-hooks/exhaustive-deps
  const [queries, setQueries] = useState<Q[]>(init.queries);
  const [active, setActive] = useState(init.queries[0].id);
  const [settings, setSettings] = useState<Settings>(init.settings);
  const [nonce, setNonce] = useState(0);
  const [copied, setCopied] = useState(false);
  const [adding, setAdding] = useState(false);
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick, nonce]);   // eslint-disable-line react-hooks/exhaustive-deps
  const step = bucketSeconds(ctx.range);
  const metricList = useQuery({ signal: "metrics", ...w, group_by: ["metric_name", "metric_type", "is_monotonic"], aggs: [{ fn: "count" }], limit: 2000 },
                              "ml" + ctx.range.key + ctx.tick);
  const kinds = useMemo(() => new Map((metricList.data ? records(metricList.data) : []).map((r) => [String(r.metric_name),
    r.metric_type === "sum" && r.is_monotonic ? "counter" : String(r.metric_type).includes("histogram") ? "histogram" : "gauge"])), [metricList.data]);
  const texts = queries.map((q) => toPromQL(q, kinds.get(q.b.metric)));
  const [ran, setRan] = useState<string[]>(texts);

  // Keep the link current, so the page can be bookmarked or shared.
  useEffect(() => {
    const blob = btoa(unescape(encodeURIComponent(JSON.stringify({ queries, settings }))));
    history.replaceState(null, "", `#/query?q=${blob}`);
  }, [queries, settings]);
  // Builder queries re-run as they change; PromQL runs on Run (Ctrl+Enter).
  useEffect(() => {
    setRan((old) => queries.map((q, i) => (q.mode === "promql" ? old[i] ?? texts[i] : texts[i])));
  }, [JSON.stringify(queries.map((q) => (q.mode === "promql" ? q.id : texts))), kinds.size]);   // eslint-disable-line react-hooks/exhaustive-deps
  const runPromQL = () => { setRan(texts); setNonce((n) => n + 1); };

  const results = useResults(queries, ran, w, step, ctx.tick + ":" + nonce);
  const update = (id: number, f: (q: Q) => Q) => setQueries((qs) => qs.map((q) => (q.id === id ? f(q) : q)));
  const cur = queries.find((q) => q.id === active) ?? queries[0];
  const idx = queries.indexOf(cur);

  // Chart series: every visible query's series, named by the query's legend template.
  const series: (Series & { last: number; avg: number; max: number; q: number })[] = [];
  queries.forEach((q, qi) => {
    if (q.hidden) return;
    for (const s of results[qi]?.data ?? []) {
      const pts = s.values.map(([t, v]) => [t * 1000, Number(v)] as [number, number]).filter((p) => isFinite(p[1]));
      if (!pts.length) continue;
      const vals = pts.map((p) => p[1]);
      series.push({ label: legendName(q, qi, s, queries.length), color: PALETTE[series.length % PALETTE.length], points: pts, q: qi,
                    last: vals[vals.length - 1], avg: vals.reduce((a, v) => a + v, 0) / vals.length, max: Math.max(...vals) });
    }
  });
  const sorted = [...series].sort((a, b) => (settings.sort === "name" ? a.label.localeCompare(b.label) : b.avg - a.avg));
  const unit = settings.unit || (queries.length === 1 ? unitOf(queries[0]) ?? "" : "");
  const fmt = (v: number) => (settings.decimals ? v.toFixed(Number(settings.decimals)) : fmtNum(v)) + unit;
  const loading = results.some((r) => r?.loading);
  const errors = results.map((r, i) => r?.error && !queries[i].hidden ? `${queries[i].name || `Query ${i + 1}`}: ${r.error}` : null).filter(Boolean);
  const threshold = settings.threshold === "" ? null : Number(settings.threshold);

  return (
    <>
      <div className="page-head">
        <h1>Query Builder</h1>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn" onClick={() => { navigator.clipboard?.writeText(location.href); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>
          {copied ? "Link copied" : "Copy link"}</button>
        <button className="btn" disabled={!texts[idx]?.trim()} title="Alert when this query crosses a threshold"
                onClick={() => ctx.go(`/alerts/rules/new?name=${encodeURIComponent(cur.name.replace(/\{\{[^}]*\}\}/g, "").trim() || "")}`
                  + `&promql=${encodeURIComponent(texts[idx].replace(/\$__rate_interval|\$__interval/g, "5m"))}`)}>Create check rule</button>
        <button className="btn" disabled={!texts.some((t, i) => t.trim() && !queries[i].hidden)} onClick={() => setAdding(true)}>Add to dashboard</button>
      </div>
      <div className="qb">
        <div className="qb-main">
          <section className="panel">
            <div className="panel-body">
              {errors.length > 0 && <div className="form-error" style={{ marginBottom: 8 }}>{errors.map((e) => <div key={e}>{e}</div>)}</div>}
              {loading && !series.length ? <div className="skeleton" style={{ height: 260 }} />
                : !series.length ? <div className="state" style={{ minHeight: 260 }}>{errors.length ? "No result" : "No data in this time range"}</div>
                : settings.chart === "bars" ? (
                  <StackedBars bars={barsOf(series)} keys={series.map((s) => ({ label: s.label, color: s.color }))} range={ctx.range}
                               bucketMs={step * 1000} height={260} unit={unit} legend={false} />
                ) : (
                  <TimeSeries series={series} range={ctx.range} height={260} unit={unit} area={settings.chart === "area"}
                              threshold={threshold} legend={false} />
                )}
              {settings.legend && series.length > 0 && (
                <div className="table-scroll" style={{ maxHeight: 180, marginTop: 8 }}>
                  <table className="dtable qb-legend">
                    <colgroup><col /><col style={{ width: 110 }} /><col style={{ width: 110 }} /><col style={{ width: 110 }} /></colgroup>
                    <thead><tr><th>Series</th><th className="num">Last</th><th className="num">Average</th><th className="num">Max</th></tr></thead>
                    <tbody>{sorted.map((s) => (
                      <tr key={s.label} style={{ cursor: "default" }}>
                        <td title={s.label}><i className="swatch" style={{ background: s.color }} />{s.label}</td>
                        <td className="num">{fmt(s.last)}</td><td className="num">{fmt(s.avg)}</td><td className="num">{fmt(s.max)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </div>
          </section>

          <section className="panel">
            <div className="tabs">
              {queries.map((q, i) => (
                <button key={q.id} type="button" className={q.id === cur.id ? "on" : undefined} onClick={() => setActive(q.id)}>
                  <span style={{ opacity: q.hidden ? 0.4 : 1 }}>{q.name ? q.name.replace(/\{\{[^}]*\}\}/g, "…") : `Query ${i + 1}`}</span>
                </button>
              ))}
              <button type="button" title="Add a query" onClick={() => {
                const id = Math.max(...queries.map((q) => q.id)) + 1;
                setQueries([...queries, newQuery(id)]); setActive(id);
              }}>＋</button>
              <span className="spacer" />
              <button type="button" className="btn small" onClick={() => update(cur.id, (q) => ({ ...q, hidden: !q.hidden }))}>{cur.hidden ? "Show" : "Hide"}</button>
              {queries.length > 1 && <button type="button" className="btn small" style={{ marginLeft: 6 }} onClick={() => {
                const rest = queries.filter((q) => q.id !== cur.id); setQueries(rest); setActive(rest[0].id);
              }}>Remove</button>}
            </div>
            <div className="panel-body qb-form">
              <label className="qb-row"><span>Query name</span>
                <input className="input mono grow" placeholder="Use placeholders like {{service_name}} to put label values in the legend" value={cur.name}
                       onChange={(e) => update(cur.id, (q) => ({ ...q, name: e.target.value }))} /></label>
              <div className="qb-row"><span>Query on</span>
                <div className="seg">
                  {([["tracing", "Tracing"], ["logging", "Logging"], ["metrics", "Metrics"], ["promql", "PromQL"]] as [Mode, string][]).map(([m, l]) => (
                    <button key={m} type="button" className={cur.mode === m ? "on" : undefined} onClick={() => update(cur.id, (q) => ({
                      ...q, mode: m, promql: m === "promql" && q.mode !== "promql" ? texts[idx] : q.promql,
                      b: m !== q.mode && m !== "promql" ? { ...EMPTY_B, filters: [], groupBy: ["service_name"], measure: m === "logging" ? "rate" : "rate" } : q.b,
                    }))}>{l}</button>
                  ))}
                </div>
              </div>
              {cur.mode === "promql" ? (
                <div className="qb-row top"><span>PromQL</span>
                  <div className="grow">
                    <textarea className="input mono qb-editor" spellCheck={false} value={cur.promql} rows={4}
                              placeholder='sum by (service_name) (rate(leasyd.spans[$__interval]))'
                              onChange={(e) => update(cur.id, (q) => ({ ...q, promql: e.target.value }))}
                              onKeyDown={(e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runPromQL(); } }} />
                    <div className="qb-editor-bar">
                      <select className="select" value="" onChange={(e) => e.target.value && update(cur.id, (q) => ({ ...q, promql: e.target.value }))} aria-label="Examples">
                        <option value="">Examples…</option>
                        {EXAMPLES.map(([l, x]) => <option key={l} value={x}>{l}</option>)}
                      </select>
                      <span className="faint">logs: <code>leasyd.logs</code> · spans: <code>leasyd.spans</code>, <code>leasyd.span.duration</code> · metrics by name · <code>$__interval</code> = the chart's step ({step}s)</span>
                      <span className="spacer" style={{ flex: 1 }} />
                      <button type="button" className="btn primary" onClick={runPromQL}>Run <span className="kbd">Ctrl ↵</span></button>
                    </div>
                  </div>
                </div>
              ) : (
                <BuilderForm q={cur} metrics={[...kinds.keys()].sort()} kind={kinds.get(cur.b.metric)}
                             onChange={(b) => update(cur.id, (q) => ({ ...q, b }))} />
              )}
              {cur.mode !== "promql" && texts[idx] && (
                <div className="qb-row top"><span>PromQL</span>
                  <div className="grow">
                    <pre className="pre qb-generated">{texts[idx]}</pre>
                    <button type="button" className="linkbtn" onClick={() => update(cur.id, (q) => ({ ...q, mode: "promql", promql: texts[idx] }))}>Edit as PromQL</button>
                  </div>
                </div>
              )}
            </div>
          </section>
        </div>

        <aside className="qb-side">
          <SideSection title="Chart type">
            <div className="seg small">{(["line", "area", "bars"] as const).map((c) => (
              <button key={c} type="button" className={settings.chart === c ? "on" : undefined} onClick={() => setSettings({ ...settings, chart: c })}>
                {c === "bars" ? "Stacked bars" : c[0].toUpperCase() + c.slice(1)}</button>))}</div>
          </SideSection>
          <SideSection title="Data & formatting">
            <label className="side-field">Unit
              <select className="select" value={settings.unit} onChange={(e) => setSettings({ ...settings, unit: e.target.value })}>
                <option value="">Automatic</option><option value=" ms">Milliseconds</option><option value=" s">Seconds</option>
                <option value="%">Percent</option><option value="/s">Per second</option><option value=" B">Bytes</option>
              </select></label>
            <label className="side-field">Decimals
              <select className="select" value={settings.decimals} onChange={(e) => setSettings({ ...settings, decimals: e.target.value })}>
                <option value="">Automatic</option>{[0, 1, 2, 3].map((d) => <option key={d} value={d}>{d}</option>)}
              </select></label>
          </SideSection>
          <SideSection title="Legend">
            <label className="side-check"><input type="checkbox" checked={settings.legend} onChange={(e) => setSettings({ ...settings, legend: e.target.checked })} />Show the series table</label>
          </SideSection>
          <SideSection title="Sorting">
            <div className="seg small">{(["value", "name"] as const).map((s) => (
              <button key={s} type="button" className={settings.sort === s ? "on" : undefined} onClick={() => setSettings({ ...settings, sort: s })}>
                {s === "value" ? "By average" : "By name"}</button>))}</div>
          </SideSection>
          <SideSection title="Thresholds">
            <label className="side-field">Line at
              <input className="input mono" inputMode="decimal" placeholder="e.g. 500" value={settings.threshold}
                     onChange={(e) => setSettings({ ...settings, threshold: e.target.value.replace(/[^0-9.\-]/g, "") })} /></label>
          </SideSection>
          <SideSection title="Limits">
            <div className="faint" style={{ fontSize: 12 }}>
              Step {step}s for {ctx.range.label.toLowerCase()}; at most 10,000 series per query. Aggregate with <code>sum by (…)</code> to see fewer.
              {results.some((r) => r?.stats) && <div style={{ marginTop: 4 }}>{results.filter((r) => r?.stats).map((r, i) => (
                <div key={i}>Query {i + 1}: {r!.stats!.engine_queries} engine quer{r!.stats!.engine_queries === 1 ? "y" : "ies"}, {fmtNum((r!.stats!.bytes ?? 0) / 1e6)} MB read</div>))}</div>}
            </div>
          </SideSection>
        </aside>
      </div>
      {adding && <AddToDashboard ctx={ctx} onClose={() => setAdding(false)}
                                 panel={{ type: settings.chart === "bars" ? "bars" : "timeseries", unit: unit.trim(),
                                          queries: queries.map((q, i) => ({ promql: texts[i], legend: q.name })).filter((q, i) => q.promql.trim() && !queries[i].hidden),
                                          title: queries.find((q) => !q.hidden && q.name)?.name.replace(/\{\{[^}]*\}\}/g, "").trim() || "" }} />}
    </>
  );
}

/** Puts the chart on a dashboard (an existing one, or a new one) as a panel. */
function AddToDashboard({ ctx, panel, onClose }: { ctx: Ctx; panel: Partial<Panel>; onClose: () => void }) {
  const [list, setList] = useState<DashboardSummary[] | null>(null);
  const [target, setTarget] = useState("new");
  const [name, setName] = useState("My dashboard");
  const [title, setTitle] = useState(panel.title || "New panel");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { dashboards.list().then((r) => { setList(r.items); if (r.items.length) setTarget(r.items[0].id); }, (e: Error) => setError(e.message)); }, []);
  const units: Record<string, string> = { "ms": "ms", "s": "s", "%": "%", "/s": "/s", "B": "bytes" };
  const add = async () => {
    setBusy(true); setError(null);
    const p: Panel = { id: Math.random().toString(16).slice(2, 10), type: panel.type ?? "timeseries", title: title.trim(), description: "",
                       w: 6, h: 2, unit: units[(panel.unit ?? "").trim()] ?? "", queries: panel.queries };
    try {
      let id = target;
      if (target === "new") {
        id = (await dashboards.create({ name: name.trim(), description: "", variables: [{ name: "service_name", label: "Service Name" }], panels: [p] })).id;
      } else {
        const d = await dashboards.get(target);
        await dashboards.update(target, { panels: [...d.panels, p], version: d.version });
      }
      ctx.go(`/dashboards/${id}`);
    } catch (e) { setError((e as Error).message); setBusy(false); }
  };
  return (
    <Drawer title="Add to dashboard" onClose={onClose}
            right={<button className="btn primary" disabled={busy || !title.trim() || (target === "new" && !name.trim())} onClick={add}>{busy ? "Adding…" : "Add"}</button>}>
      <div className="drawer-section form-grid" style={{ gridTemplateColumns: "1fr" }}>
        <label>Dashboard
          <select className="select" value={target} onChange={(e) => setTarget(e.target.value)}>
            {(list ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            <option value="new">New dashboard…</option>
          </select></label>
        {target === "new" && <label>New dashboard's name<input className="input" maxLength={100} value={name} onChange={(e) => setName(e.target.value)} /></label>}
        <label>Panel title<input className="input" maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <div className="faint">The panel keeps this chart's {panel.queries?.length ?? 0} quer{panel.queries?.length === 1 ? "y" : "ies"}; <code>$__interval</code> follows the dashboard's time range. Add <code>{'service_name=~"$service_name"'}</code> to a selector to make it follow the dashboard's service filter.</div>
        {error && <div className="form-error">{error}</div>}
      </div>
    </Drawer>
  );
}

function SideSection({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(title === "Chart type" || title === "Data & formatting");
  return (
    <div className="side-section">
      <button type="button" className="side-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`caret${open ? " open" : ""}`}>▸</span>{title}
      </button>
      {open && <div className="side-body">{children}</div>}
    </div>
  );
}

const TRACING_FILTER_LABELS = TRACING_LABELS;
function BuilderForm({ q, metrics, kind, onChange }: { q: Q; metrics: string[]; kind?: string; onChange: (b: Builder) => void }) {
  const b = q.b;
  const labels = q.mode === "tracing" ? TRACING_FILTER_LABELS : q.mode === "logging" ? LOGGING_LABELS : METRIC_LABELS;
  const measures = q.mode === "tracing" ? TRACING_MEASURES : q.mode === "logging" ? LOGGING_MEASURES : null;
  return (
    <>
      {measures ? (
        <div className="qb-row"><span>Measure</span>
          <div className="chips">{measures.map(([k, l]) => (
            <button key={k} type="button" className={`chip${b.measure === k ? " on" : ""}`} onClick={() => onChange({ ...b, measure: k })}>{l}</button>))}</div>
        </div>
      ) : (
        <div className="qb-row"><span>Metric</span>
          <div className="grow" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input className="input mono grow" list="qb-metrics" placeholder="metric name" value={b.metric} onChange={(e) => onChange({ ...b, metric: e.target.value })} />
            <datalist id="qb-metrics">{metrics.map((m) => <option key={m} value={m} />)}</datalist>
            {kind === "histogram" ? (
              <select className="select" value={/^p\d+$/.test(b.agg) ? b.agg : "avg"} onChange={(e) => onChange({ ...b, agg: e.target.value })} aria-label="Aggregate">
                {[["avg", "average"], ["p50", "p50"], ["p90", "p90"], ["p95", "p95"], ["p99", "p99"]].map(([a, l]) => <option key={a} value={a}>{l}</option>)}
              </select>
            ) : (
              <select className="select" value={/^p\d+$/.test(b.agg) ? "sum" : b.agg} onChange={(e) => onChange({ ...b, agg: e.target.value })} aria-label="Aggregate">
                {["sum", "avg", "min", "max"].map((a) => <option key={a} value={a}>{a}</option>)}
              </select>)}
            <span className="faint">{kind === "counter" ? "counter: rate per second" : kind === "histogram" ? (/^p\d+$/.test(b.agg) ? "histogram: percentile from its buckets, in its unit" : "histogram: average") : kind ? "gauge: average over each step" : ""}</span>
          </div>
        </div>
      )}
      <div className="qb-row top"><span>Filter</span>
        <div className="grow">
          {b.filters.map((f, i) => (
            <div key={i} className="qb-filter">
              <input className="input mono" list="qb-labels" placeholder="label" value={f.label}
                     onChange={(e) => onChange({ ...b, filters: b.filters.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) })} />
              <select className="select" value={f.op} onChange={(e) => onChange({ ...b, filters: b.filters.map((x, j) => (j === i ? { ...x, op: e.target.value as Filter["op"] } : x)) })}>
                {["=", "!=", "=~", "!~"].map((o) => <option key={o} value={o}>{o === "=~" ? "matches" : o === "!~" ? "doesn't match" : o}</option>)}
              </select>
              <input className="input mono grow" placeholder={f.label === "span_kind" ? "SERVER" : f.label === "status_code" ? "ERROR" : f.label === "severity_range" ? "ERROR_FATAL" : "value"} value={f.value}
                     onChange={(e) => onChange({ ...b, filters: b.filters.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)) })} />
              <button type="button" className="btn" aria-label="Remove" onClick={() => onChange({ ...b, filters: b.filters.filter((_, j) => j !== i) })}>✕</button>
            </div>
          ))}
          <button type="button" className="linkbtn" onClick={() => onChange({ ...b, filters: [...b.filters, { label: labels[0], op: "=", value: "" }] })}>+ Add filter</button>
          <datalist id="qb-labels">{labels.map((l) => <option key={l} value={l} />)}</datalist>
        </div>
      </div>
      <div className="qb-row"><span>Group by</span>
        <div className="chips">
          {b.groupBy.map((g) => <button key={g} type="button" className="chip on" onClick={() => onChange({ ...b, groupBy: b.groupBy.filter((x) => x !== g) })}>{g} ✕</button>)}
          <select className="select" value="" onChange={(e) => e.target.value && onChange({ ...b, groupBy: [...b.groupBy, e.target.value] })} aria-label="Add group">
            <option value="">+ label</option>
            {labels.filter((l) => !b.groupBy.includes(l)).map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>
      </div>
    </>
  );
}

type Res = { data: PromSeries[] | null; error: string | null; loading: boolean; stats?: Record<string, number> };

/** Runs each query (with $__interval filled in) when its text, the range or the refresh tick changes. */
function useResults(queries: Q[], texts: string[], w: { start: string; end: string }, step: number, tick: string): Res[] {
  const [state, setState] = useState<Record<string, Res>>({});
  const keys = texts.map((t) => `${t}|${w.start}|${w.end}|${step}|${tick}`);
  useEffect(() => {
    let live = true;
    keys.forEach((k, i) => {
      if (state[k] || !texts[i]?.trim() || queries[i]?.hidden) return;
      setState((s) => ({ ...s, [k]: { data: null, error: null, loading: true } }));
      const text = texts[i].replace(/\$__rate_interval|\$__interval/g, `${step}s`);
      promql({ promql: text, start: w.start, end: w.end, step }).then(
        (r) => live && setState((s) => ({ ...s, [k]: { data: r.data.result, error: null, loading: false, stats: r.stats } })),
        (e: Error) => live && setState((s) => ({ ...s, [k]: { data: null, error: e.message, loading: false } })),
      );
    });
    return () => { live = false; };
  }, [keys.join("\n"), queries.map((q) => q.hidden).join()]);   // eslint-disable-line react-hooks/exhaustive-deps
  return keys.map((k) => state[k]);
}

function legendName(q: Q, qi: number, s: PromSeries, nq: number): string {
  const labels = Object.entries(s.metric).filter(([k]) => k !== "__name__");
  if (q.name) return q.name.replace(/\{\{\s*([^}\s]+)\s*\}\}/g, (_, k) => s.metric[k] ?? s.metric[k.replace(/_/g, ".")] ?? "");
  const base = labels.length ? labels.map(([, v]) => v).join(" · ") : s.metric.__name__ ?? `Query ${qi + 1}`;
  return nq > 1 ? `${qi + 1}: ${base}` : base;
}

function barsOf(series: Series[]) {
  const times = [...new Set(series.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
  return times.map((t) => ({ t, values: series.map((s) => s.points.find((p) => p[0] === t)?.[1] ?? 0) }));
}

