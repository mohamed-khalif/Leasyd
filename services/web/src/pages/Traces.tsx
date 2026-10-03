import { FormEvent, useMemo, useState } from "react";
import { records, Where } from "../api";
import type { Ctx } from "../App";
import { bucketLow, Cell, Column, ColumnPicker, Heatmap, Tabs, useColumns, View, ViewsSidebar } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtMs, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { fmtTs } from "./Logs";

const SERVICE_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
const LOOKBACK_DAYS = 30;   // trace ids are looked up across 30 days (day/hour ID filters make this fast)

export function Traces({ ctx, traceId, service }: { ctx: Ctx; traceId?: string; service?: string }) {
  const [id, setId] = useState(traceId ?? "");
  const submit = (e: FormEvent) => { e.preventDefault(); if (id.trim()) ctx.go(`/traces/${id.trim().toLowerCase()}`); };
  if (!traceId) return <Explorer ctx={ctx} service={service} />;
  return (
    <>
      <form className="toolbar" onSubmit={submit}>
        <a href="#/traces" className="btn" onClick={(e) => { e.preventDefault(); ctx.go("/traces"); }}>← All spans</a>
        <input className="input grow mono" placeholder="Trace ID (32 hex characters)" value={id} onChange={(e) => setId(e.target.value)} aria-label="Trace ID" />
        <button className="btn primary">Open trace</button>
      </form>
      <Waterfall ctx={ctx} traceId={traceId} />
    </>
  );
}

// ------------------------------------------------------------------ explorer

const VIEWS: (View & { where: Where[]; services?: string[] })[] = [
  { key: "all", label: "All spans", where: [] },
  { key: "roots", label: "All traces", hint: "Root spans: one per trace", where: [{ field: "parent_span_id", op: "not_exists" }] },
  { key: "errors", label: "Errors", where: [{ field: "status_code", op: "=", value: 2 }] },
  { key: "db", label: "Database queries", where: [{ field: "attributes.db.system", op: "exists" }] },
  { key: "genai", label: "Gen AI", where: [{ field: "attributes.gen_ai.system", op: "exists" }] },
  { key: "grpc", label: "gRPC", where: [{ field: "attributes.rpc.system", op: "=", value: "grpc" }] },
  { key: "http", label: "HTTP", where: [{ field: "attributes.http.request.method", op: "exists" }] },
  { key: "server", label: "Service requests", hint: "Server spans: requests your services received", where: [{ field: "kind", op: "=", value: 2 }] },
  { key: "synthetics", label: "Synthetic check runs", group: "Leasyd", where: [{ field: "parent_span_id", op: "not_exists" }], services: ["synthetics"] },
];
const KINDS = ["Unspecified", "Internal", "Server", "Client", "Producer", "Consumer"];
const kindName = (k: unknown) => {
  const n = Number(k);
  if (Number.isInteger(n) && KINDS[n]) return KINDS[n];
  const s = String(k ?? "").replace("SPAN_KIND_", "");
  return s ? s[0] + s.slice(1).toLowerCase() : "—";
};
function spanType(a: Record<string, unknown> | undefined): string {
  if (!a) return "—";
  if (a["db.system"] != null) return `DB · ${a["db.system"]}`;
  if (a["gen_ai.system"] != null) return "Gen AI";
  if (a["rpc.system"] != null) return a["rpc.system"] === "grpc" ? "gRPC" : "RPC";
  if (a["http.request.method"] != null || a["http.method"] != null) return "HTTP";
  if (a["messaging.system"] != null) return "Messaging";
  return "—";
}
const COLUMNS: Column[] = [
  { key: "name", label: "Name" }, { key: "service", label: "Service" }, { key: "start", label: "Start time" },
  { key: "duration", label: "Duration" }, { key: "root", label: "Root" }, { key: "type", label: "Type" },
  { key: "kind", label: "Kind" }, { key: "status", label: "Status" },
];
const WIDTH: Record<string, number | undefined> = { service: 150, start: 190, duration: 100, root: 60, type: 110, kind: 90, status: 70 };
const LIMIT = 200;

function Explorer({ ctx, service: initialService }: { ctx: Ctx; service?: string }) {
  const [view, setView] = useState("all");
  const [chart, setChart] = useState<"outliers" | "red">("outliers");
  const [service, setService] = useState(initialService ?? "");
  const [text, setText] = useState(""), [applied, setApplied] = useState("");
  const [cell, setCell] = useState<{ t: number; b: number } | null>(null);
  const [sort, setSort] = useState<"start" | "duration">("start");
  const [id, setId] = useState("");
  const [shown, setShown] = useColumns("spans", COLUMNS);

  const v = VIEWS.find((x) => x.key === view) ?? VIEWS[0];
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const b = bucketSeconds(ctx.range);
  const where: Where[] = [...v.where, ...(applied.trim() ? [{ field: "name", op: "contains", value: applied.trim() }] : [])];
  const services = v.services ?? (service ? [service] : undefined);
  const base = { signal: "traces" as const, ...w, where, ...(services ? { services } : {}) };
  const key = JSON.stringify(base) + ctx.tick;

  const heat = useQuery(chart === "outliers" ? { ...base, group_by: [`ts:${b}`, "log:duration_ns", "status_code"], aggs: [{ fn: "count" }], limit: 10000 } : null, "h" + key + chart);
  const red = useQuery(chart === "red" ? { ...base, group_by: [`ts:${b}`], aggs: [{ fn: "count" }, { fn: "p50", field: "duration_ns" }, { fn: "p95", field: "duration_ns" }, { fn: "p99", field: "duration_ns" }], limit: 10000 } : null, "R" + key + chart);
  const redErr = useQuery(chart === "red" ? { ...base, where: [...where, { field: "status_code", op: "=", value: 2 }], group_by: [`ts:${b}`], aggs: [{ fn: "count" }], limit: 10000 } : null, "E" + key + chart);
  // The table: the selected heat map cell's spans (that time, that long), else the newest.
  const tableWhere: Where[] = cell ? [...where, { field: "duration_ns", op: ">=", value: Math.floor(bucketLow(cell.b)) }, { field: "duration_ns", op: "<", value: Math.ceil(bucketLow(cell.b + 1)) }] : where;
  const tableWindow = cell ? { start: new Date(cell.t).toISOString(), end: new Date(cell.t + b * 1000).toISOString() } : w;
  const spans = useQuery({ ...base, ...tableWindow, where: tableWhere, search: { limit: LIMIT } }, "s" + key + JSON.stringify(cell));
  const svcList = useQuery({ signal: "traces", ...w, group_by: ["service"], aggs: [{ fn: "count" }], limit: 100 }, "v" + ctx.range.key + ctx.tick);

  const cells: Cell[] = useMemo(() => {
    const m = new Map<string, Cell>();
    for (const r of heat.data ? records(heat.data) : []) {
      if (r["log:duration_ns"] == null) continue;
      const t = Date.parse(String(r[`ts:${b}`])), bk = Number(r["log:duration_ns"]), k = `${t}:${bk}`;
      if (!m.has(k)) m.set(k, { t, b: bk, count: 0, errors: 0 });
      const c = m.get(k)!;
      c.count += Number(r.count);
      if (isError(String(r.status_code))) c.errors += Number(r.count);
    }
    return [...m.values()];
  }, [heat.data, b]);
  const total = cells.reduce((a, c) => a + c.count, 0), errors = cells.reduce((a, c) => a + c.errors, 0);
  const rows = (spans.data ? records(spans.data) : []).sort((x, y) => sort === "duration" ? Number(y.duration_ns) - Number(x.duration_ns) : String(y.ts).localeCompare(String(x.ts)));
  const redRows = red.data ? records(red.data) : [];
  const pts = (f: (r: Record<string, unknown>) => number | null) =>
    redRows.map((r) => [Date.parse(String(r[`ts:${b}`])), f(r)] as [number, number | null]).filter((p): p is [number, number] => p[1] != null);
  const errAt = new Map((redErr.data ? records(redErr.data) : []).map((r) => [String(r[`ts:${b}`]), Number(r.count)]));
  const toMs = (x: unknown) => (x == null ? null : Number(x) / 1e6);

  return (
    <div className="explorer">
      <ViewsSidebar views={VIEWS} active={view} onPick={(k) => { setView(k); setCell(null); }} />
      <div className="explorer-main">
        <div className="page-head">
          <h1>{v.label}</h1>
          {chart === "outliers" && heat.data && <>
            <span className="head-count"><b>{fmtNum(total)}</b>spans</span>
            <span className="head-count bad"><b>{fmtNum(errors)}</b>errors{total ? ` (${((errors / total) * 100).toFixed(1)}%)` : ""}</span>
          </>}
        </div>
        <form className="toolbar" onSubmit={(e) => { e.preventDefault(); setApplied(text); setCell(null); }}>
          <input className="input grow mono" placeholder="Filter by span name…" value={text} onChange={(e) => setText(e.target.value)} aria-label="Filter by span name" />
          {!v.services && (
            <select className="select" value={service} onChange={(e) => { setService(e.target.value); setCell(null); }} aria-label="Service">
              <option value="">All services</option>
              {(svcList.data ? records(svcList.data) : []).map((r) => <option key={String(r.service)} value={String(r.service)}>{String(r.service)}</option>)}
            </select>
          )}
          <button className="btn primary">Filter</button>
          <span className="faint" style={{ marginLeft: 12 }}>or open</span>
          <input className="input mono" style={{ width: 230 }} placeholder="a trace ID" value={id} onChange={(e) => setId(e.target.value)} aria-label="Trace ID"
                 onKeyDown={(e) => { if (e.key === "Enter" && id.trim()) { e.preventDefault(); ctx.go(`/traces/${id.trim().toLowerCase()}`); } }} />
        </form>

        <section className="panel">
          <Tabs tabs={[["outliers", "Outliers"], ["red", "Rate, errors, duration"]]} active={chart} onPick={(c) => { setChart(c); setCell(null); }}
                right={chart === "outliers" && <span className="faint" style={{ fontSize: 12 }}>{cell ? <button type="button" className="linkbtn" onClick={() => setCell(null)}>clear selection ✕</button> : "click a cell to list its spans · red: errors"}</span>} />
          <div className="panel-body">
            {chart === "outliers" ? (
              <Loads q={heat} empty={!cells.length} height={200}>
                {() => <Heatmap cells={cells} range={ctx.range} bucketMs={b * 1000} height={200} onCell={(c) => setCell(cell && cell.t === c.t && cell.b === c.b ? null : c)} selected={cell} />}
              </Loads>
            ) : (
              <Loads q={red} empty={!redRows.length} height={200}>
                {() => (
                  <div className="grid">
                    <div className="span-4"><div className="faint">Requests per second</div>
                      <TimeSeries series={[{ label: "spans/s", color: "var(--series-1)", points: pts((r) => Number(r.count) / b) }]} range={ctx.range} height={170} /></div>
                    <div className="span-4"><div className="faint">Errors</div>
                      <TimeSeries series={[{ label: "% errors", color: "var(--sev-error)", points: pts((r) => (Number(r.count) ? ((errAt.get(String(r[`ts:${b}`])) ?? 0) / Number(r.count)) * 100 : 0)) }]} range={ctx.range} unit="%" height={170} /></div>
                    <div className="span-4"><div className="faint">Duration</div>
                      <TimeSeries area={false} series={[{ label: "p50", color: "var(--series-1)", points: pts((r) => toMs(r["p50(duration_ns)"])) },
                                                        { label: "p95", color: "var(--series-3)", points: pts((r) => toMs(r["p95(duration_ns)"])) },
                                                        { label: "p99", color: "var(--series-5)", points: pts((r) => toMs(r["p99(duration_ns)"])) }]} range={ctx.range} unit=" ms" height={170} /></div>
                  </div>
                )}
              </Loads>
            )}
          </div>
        </section>

        <section className="panel">
          <div className="tabs">
            <span style={{ padding: "8px 4px", fontSize: 12.5 }}>{cell ? `Spans at ${new Date(cell.t).toLocaleTimeString()} taking ${fmtMs(bucketLow(cell.b))} – ${fmtMs(bucketLow(cell.b + 1))}` : "Spans"}</span>
            <span className="faint" style={{ fontSize: 12, marginLeft: 8 }}>{spans.data ? (rows.length >= LIMIT ? `newest ${LIMIT}` : `${rows.length}`) : ""}</span>
            <span className="spacer" />
            <ColumnPicker columns={COLUMNS} shown={shown} onChange={setShown} />
          </div>
          <Loads q={spans} empty={!rows.length} height={200}>
            {() => (
              <div className="table-scroll" style={{ maxHeight: 620 }}>
                <table className="dtable">
                  <colgroup>{COLUMNS.filter((c) => shown.includes(c.key)).map((c) => <col key={c.key} style={{ width: WIDTH[c.key] }} />)}</colgroup>
                  <thead><tr>{COLUMNS.filter((c) => shown.includes(c.key)).map((c) => (
                    <th key={c.key} className={c.key === "duration" ? "num" : undefined}
                        style={c.key === "start" || c.key === "duration" ? { cursor: "pointer" } : undefined}
                        onClick={() => (c.key === "start" || c.key === "duration") && setSort(c.key)}>
                      {c.label}{sort === c.key ? " ↓" : ""}
                    </th>))}</tr></thead>
                  <tbody>
                    {rows.map((r, i) => {
                      const a = r.attributes as Record<string, unknown> | undefined, err = isError(String(r.status_code));
                      return (
                        <tr key={i} onClick={() => ctx.go(`/traces/${r.trace_id}`)} title="Open the trace">
                          {shown.includes("name") && <td><span className="sev-pill" style={{ background: err ? "var(--sev-error)" : "var(--accent)" }} />{String(r.name ?? "")}</td>}
                          {shown.includes("service") && <td className="muted">{String(r.service)}</td>}
                          {shown.includes("start") && <td className="faint">{fmtTs(String(r.ts))}</td>}
                          {shown.includes("duration") && <td className="num">{fmtMs(Number(r.duration_ns))}</td>}
                          {shown.includes("root") && <td>{r.parent_span_id ? "" : "✓"}</td>}
                          {shown.includes("type") && <td className="muted">{spanType(a)}</td>}
                          {shown.includes("kind") && <td className="muted">{kindName(r.kind)}</td>}
                          {shown.includes("status") && <td style={err ? { color: "var(--sev-error)" } : undefined}>{err ? "error" : "ok"}</td>}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Loads>
        </section>
      </div>
    </div>
  );
}

type SpanEvent = { at: number; name: string; attributes: Record<string, string> };   // at: ns since the epoch
type Span = { span_id: string; parent_span_id?: string; name: string; service: string; start: number; dur: number; status?: string; depth: number;
              events: SpanEvent[]; raw: Record<string, unknown> };

/** A span's events (OTLP span events: name, time, attributes), oldest first. */
function spanEvents(r: Record<string, unknown>, start: number): SpanEvent[] {
  const list = Array.isArray(r.events) ? (r.events as Record<string, unknown>[]) : [];
  return list.map((e) => {
    const iso = String(e.ts ?? ""), micros = Number((iso.match(/\.(\d+)Z$/)?.[1] ?? "").padEnd(6, "0").slice(3, 6));
    const ms = Date.parse(iso);
    return { at: isFinite(ms) ? ms * 1e6 + (isFinite(micros) ? micros * 1e3 : 0) : start, name: String(e.name ?? ""),
             attributes: (e.attributes && typeof e.attributes === "object" ? e.attributes : {}) as Record<string, string> };
  }).sort((a, b) => a.at - b.at);
}
const isException = (e: SpanEvent) => e.name === "exception";

function Waterfall({ ctx, traceId }: { ctx: Ctx; traceId: string }) {
  const w = useMemo(() => ({ start: new Date(Date.now() - LOOKBACK_DAYS * 86_400_000).toISOString(), end: new Date().toISOString() }), [traceId, ctx.tick]);
  const spans = useQuery({ signal: "traces", ...w, match: { trace_id: traceId }, search: { limit: 2000 } }, "t" + traceId + ctx.tick);
  const logs = useQuery({ signal: "logs", ...w, match: { trace_id: traceId }, search: { limit: 200 } }, "l" + traceId + ctx.tick);
  const [sel, setSel] = useState<string | null>(null);

  const tree = useMemo(() => (spans.data ? layout(records(spans.data)) : []), [spans.data]);
  const t0 = Math.min(...tree.map((s) => s.start)), t1 = Math.max(...tree.map((s) => s.start + s.dur));
  const services = [...new Set(tree.map((s) => s.service))];
  const color = (svc: string) => SERVICE_COLORS[services.indexOf(svc) % SERVICE_COLORS.length];
  const selected = tree.find((s) => s.span_id === sel);

  return (
    <>
      <Panel title={`Trace ${traceId}`} flush
             right={tree.length > 0 && <span className="faint mono">{tree.length} spans · {services.length} services · {fmtMs(t1 - t0)}
               <button type="button" className="btn ask-btn" onClick={() => ctx.go(`/ai?ask=${encodeURIComponent(`Explain trace \`${traceId}\`: what happened, what was slow or failed, and why?`)}&page=${encodeURIComponent(JSON.stringify({ trace_id: traceId }))}`)}>Ask Leasyd AI</button></span>}>
        <Loads q={spans} empty={!tree.length} height={200}>
          {() => (
            <div>
              <div className="legend" style={{ padding: "8px 14px 0" }}>{services.map((s) => <span key={s}><i style={{ background: color(s) }} />{s}</span>)}</div>
              {tree.map((s) => {
                const left = ((s.start - t0) / (t1 - t0 || 1)) * 100, width = Math.max((s.dur / (t1 - t0 || 1)) * 100, 0.2);
                return (
                  <div key={s.span_id} className="wf-row" onClick={() => setSel(sel === s.span_id ? null : s.span_id)}
                       style={sel === s.span_id ? { background: "var(--surface-2)" } : undefined}>
                    <div className="wf-name" style={{ paddingLeft: 12 + s.depth * 14 }} title={`${s.service} ${s.name}`}>
                      <span style={{ color: color(s.service) }}>{s.service}</span> <span className="muted">{s.name}</span>
                    </div>
                    <div className="wf-track">
                      <div className="wf-bar" style={{ left: `${left}%`, width: `${width}%`, background: isError(s.status) ? "var(--sev-error)" : color(s.service) }} />
                      {s.events.map((e, j) => (
                        <span key={j} className={`wf-event${isException(e) ? " exc" : ""}`} title={`${e.name} · +${fmtMs(e.at - s.start)}`}
                              style={{ left: `${((e.at - t0) / (t1 - t0 || 1)) * 100}%` }} />
                      ))}
                      <span className="wf-dur" style={left + width > 80 ? { right: `${100 - left + 0.5}%` } : { left: `calc(${left + width}% + 6px)` }}>{fmtMs(s.dur)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Loads>
      </Panel>
      {selected && <SpanDetail span={selected} />}
      <Panel title="Logs in this trace" flush>
        <Loads q={logs} empty={!(logs.data && logs.data.rows.length)} height={80}>
          {() => (
            <div>{records(logs.data!).sort((a, b) => Number(a.ts_unix_nano) - Number(b.ts_unix_nano)).map((r, i) => (
              <div key={i} className="logrow" style={{ cursor: "default" }}>
                <span className="faint">{fmtTs(String(r.ts))}</span>
                <span className={`sev ${String(r.severity_text ?? "").toLowerCase()}`}>{String(r.severity_text ?? "—")}</span>
                <span className="muted">{String(r.service)}</span>
                <span className="body">{String(r.body ?? "")}</span>
              </div>
            ))}</div>
          )}
        </Loads>
      </Panel>
    </>
  );
}

function SpanDetail({ span }: { span: Span }) {
  const fields = Object.entries(flatten(span.raw)).filter(([k, v]) => k !== "events" && k !== "links" && v != null && v !== "");
  const linkList = Array.isArray(span.raw.links) ? (span.raw.links as Record<string, unknown>[]) : [];
  return (
    <Panel title={`Span ${span.name}`} flush right={span.events.length > 0 && <span className="faint">{span.events.length} event{span.events.length > 1 ? "s" : ""}</span>}>
      {span.events.length > 0 && (
        <div className="span-events">
          <div className="span-sec">Events</div>
          {span.events.map((e, i) => {
            const { "exception.stacktrace": stack, ...attrs } = e.attributes;
            return (
              <div key={i} className={`span-event${isException(e) ? " exc" : ""}`}>
                <div className="span-event-head">
                  <span className="mono faint">+{fmtMs(e.at - span.start)}</span>
                  <b>{isException(e) ? `${attrs["exception.type"] ?? "exception"}` : e.name}</b>
                  {isException(e) && attrs["exception.message"] && <span>{attrs["exception.message"]}</span>}
                </div>
                {Object.keys(attrs).length > 0 && (
                  <div className="kv">
                    {Object.entries(attrs).map(([k, v]) => <div key={k} style={{ display: "contents" }}><span className="k">{k}</span><span className="v">{String(v)}</span></div>)}
                  </div>
                )}
                {stack && <pre className="stack">{stack}</pre>}
              </div>
            );
          })}
        </div>
      )}
      {linkList.length > 0 && (
        <div className="span-events">
          <div className="span-sec">Links</div>
          {linkList.map((l, i) => <div key={i} className="mono" style={{ fontSize: 12 }}>trace {String(l.trace_id)} · span {String(l.span_id)}</div>)}
        </div>
      )}
      <div className="logdetail" style={{ background: "transparent" }}>
        <div className="kv">
          {fields.map(([k, v]) => <div key={k} style={{ display: "contents" }}><span className="k">{k}</span><span className="v">{String(v)}</span></div>)}
        </div>
      </div>
    </Panel>
  );
}

/** OTLP span status: stored as its number (2 = error); older data may hold the enum name. */
export const isError = (status?: string) => status === "2" || status === "STATUS_CODE_ERROR";

/** Spans in tree order (parents before children, siblings by start time) with their depth. */
function layout(rows: Record<string, unknown>[]): Span[] {
  const spans: Span[] = rows.map((r) => ({
    span_id: String(r.span_id), parent_span_id: r.parent_span_id ? String(r.parent_span_id) : undefined,
    name: String(r.name ?? ""), service: String(r.service ?? "unknown"),
    start: Number(r.ts_unix_nano), dur: Number(r.duration_ns ?? 0), status: r.status_code ? String(r.status_code) : undefined,
    depth: 0, events: spanEvents(r, Number(r.ts_unix_nano)), raw: r,
  }));
  const ids = new Set(spans.map((s) => s.span_id));
  const kids = new Map<string, Span[]>();
  const roots: Span[] = [];
  for (const s of spans) {
    if (s.parent_span_id && ids.has(s.parent_span_id)) kids.set(s.parent_span_id, [...(kids.get(s.parent_span_id) ?? []), s]);
    else roots.push(s);
  }
  const out: Span[] = [];
  const walk = (s: Span, d: number) => {
    s.depth = d; out.push(s);
    for (const c of (kids.get(s.span_id) ?? []).sort((a, b) => a.start - b.start)) walk(c, d + 1);
  };
  roots.sort((a, b) => a.start - b.start).forEach((r) => walk(r, 0));
  return out;
}

function flatten(r: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(r)) {
    if (v && typeof v === "object" && !Array.isArray(v)) for (const [k2, v2] of Object.entries(v as Record<string, unknown>)) out[`${k === "resource_attributes" ? "resource" : k}.${k2}`] = v2;
    else if (Array.isArray(v)) out[k] = v.length ? JSON.stringify(v) : null;
    else out[k] = v;
  }
  return out;
}
