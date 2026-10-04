import { useMemo, useState } from "react";
import { records, Where } from "../api";
import type { Ctx } from "../App";
import { bucketLow, Cell, LOG_STEPS, Column, ColumnPicker, Heatmap, Tabs, useColumns, View, ViewsSidebar } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtMs, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { fmtTs } from "./Logs";

const SERVICE_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
const LOOKBACK_DAYS = 30;   // trace ids are looked up across 30 days (day/hour ID filters make this fast)

export function Traces({ ctx, traceId, service }: { ctx: Ctx; traceId?: string; service?: string }) {
  if (!traceId) return <Explorer ctx={ctx} service={service} />;
  return <Waterfall ctx={ctx} traceId={traceId} />;
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
export type Span = { span_id: string; parent_span_id?: string; name: string; service: string; start: number; dur: number; status?: string; depth: number;
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
  const [closed, setClosed] = useState<Set<string>>(new Set());

  const tree = useMemo(() => (spans.data ? layout(records(spans.data)) : []), [spans.data]);
  const logRows = useMemo(() => (logs.data ? records(logs.data) : []).sort((a, b) => Number(a.ts_unix_nano) - Number(b.ts_unix_nano)), [logs.data]);
  const t0 = Math.min(...tree.map((s) => s.start)), t1 = Math.max(...tree.map((s) => s.start + s.dur));
  const services = [...new Set(tree.map((s) => s.service))];
  const color = (svc: string) => SERVICE_COLORS[services.indexOf(svc) % SERVICE_COLORS.length];
  const kids = useMemo(() => {
    const m = new Map<string, number>();
    tree.forEach((s) => s.parent_span_id && m.set(s.parent_span_id, (m.get(s.parent_span_id) ?? 0) + 1));
    return m;
  }, [tree]);
  // Rows under a collapsed span are hidden (tree order: a span's descendants follow it, deeper).
  const visible = useMemo(() => {
    const out: Span[] = [];
    let hideBelow: number | null = null;
    for (const s of tree) {
      if (hideBelow != null && s.depth > hideBelow) continue;
      hideBelow = closed.has(s.span_id) ? s.depth : null;
      out.push(s);
    }
    return out;
  }, [tree, closed]);
  const errors = tree.filter((s) => isError(s.status)).length;
  const root = tree[0];
  const selected = tree.find((s) => s.span_id === sel) ?? tree.find((s) => isError(s.status)) ?? root;
  const toggle = (id: string) => setClosed((c) => { const n = new Set(c); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const attrs = (s?: Span) => (s?.raw.attributes ?? {}) as Record<string, string>;
  const rootStatus = attrs(root)["http.response.status_code"] ?? attrs(root)["http.status_code"];
  const rootKind = spanType(attrs(root));

  return (
    <>
      <Loads q={spans} empty={!tree.length} height={260}>
        {() => (
          <>
            <div className="trace-head">
              <button className="btn" onClick={() => ctx.go("/traces")}>← Tracing</button>
              {rootKind !== "—" && <span className="trace-kind">{rootKind}</span>}
              <b className="trace-title">{root.name}</b>
              {rootStatus && <span className={Number(rootStatus) >= 500 || isError(root.status) ? "bad" : "faint"}>{rootStatus}</span>}
              {!rootStatus && isError(root.status) && <span className="bad">Error</span>}
              <span className="faint mono">#{traceId.slice(0, 16)}…</span>
              <span className="spacer" />
              <span className="faint">{tree.length} spans · {services.length} services · {fmtMs(t1 - t0)}</span>
              {errors > 0 && <span className="bad">{errors} error{errors > 1 ? "s" : ""} ({((errors / tree.length) * 100).toFixed(1)}%)</span>}
              <button type="button" className="btn ask-btn" onClick={() => ctx.go(`/ai?ask=${encodeURIComponent(`Explain trace \`${traceId}\`: what happened, what was slow or failed, and why?`)}&page=${encodeURIComponent(JSON.stringify({ trace_id: traceId }))}`)}>Ask Leasyd AI</button>
            </div>
            <div className="trace-split">
              <section className="panel trace-table" aria-label="Spans">
                <div className="tw-row tw-headrow">
                  <span>Span</span><span>Service</span><span className="num">Duration</span>
                  <span className="tw-axis"><span>0</span><span>{fmtMs((t1 - t0) / 2)}</span><span>{fmtMs(t1 - t0)}</span></span>
                </div>
                {visible.map((s) => {
                  const left = ((s.start - t0) / (t1 - t0 || 1)) * 100, width = Math.max((s.dur / (t1 - t0 || 1)) * 100, 0.3);
                  const n = kids.get(s.span_id) ?? 0, err = isError(s.status);
                  return (
                    <div key={s.span_id} className={`tw-row${selected?.span_id === s.span_id ? " on" : ""}${err ? " err" : ""}`} onClick={() => setSel(s.span_id)}>
                      <span className="tw-name" style={{ paddingLeft: 6 + s.depth * 14 }} title={s.name}>
                        {n > 0 ? <button className="tw-toggle" onClick={(e) => { e.stopPropagation(); toggle(s.span_id); }} aria-label={closed.has(s.span_id) ? "Expand" : "Collapse"}>{closed.has(s.span_id) ? "+" : "−"}</button> : <span className="tw-toggle none" />}
                        <span className={err ? "bad" : undefined}>{s.name}</span>
                        {closed.has(s.span_id) && <span className="tw-count">{n}</span>}
                        {s.events.length > 0 && <span className={`tw-ev${s.events.some(isException) ? " exc" : ""}`} title={`${s.events.length} events`}>◆{s.events.length}</span>}
                      </span>
                      <span className="tw-svc" title={s.service}><i style={{ background: color(s.service) }} />{s.service}</span>
                      <span className="num">{fmtMs(s.dur)}</span>
                      <span className="tw-track">
                        <span className="wf-bar" style={{ left: `${left}%`, width: `${width}%`, background: err ? "var(--sev-error)" : color(s.service) }} />
                        {s.events.map((e, j) => <span key={j} className={`wf-event${isException(e) ? " exc" : ""}`} style={{ left: `${((e.at - t0) / (t1 - t0 || 1)) * 100}%` }} title={e.name} />)}
                      </span>
                    </div>
                  );
                })}
              </section>
              {selected && <SpanPanel span={selected} logs={logRows.filter((l) => String(l.span_id ?? "") === selected.span_id)} />}
            </div>
          </>
        )}
      </Loads>
      <Panel title="Logs in this trace" flush>
        <Loads q={logs} empty={!logRows.length} height={80}>
          {() => (
            <div>{logRows.map((r, i) => (
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

type SpanTab = "overview" | "attributes" | "resource" | "events" | "links";

/** The selected span: when it ran, how its duration compares with the same operation's other
 *  spans, and its attributes, resource, events and logs, and links. */
function SpanPanel({ span, logs }: { span: Span; logs: Record<string, unknown>[] }) {
  const [tab, setTab] = useState<SpanTab>("overview");
  const attrs = (span.raw.attributes ?? {}) as Record<string, unknown>;
  const resource = (span.raw.resource_attributes ?? {}) as Record<string, unknown>;
  const links = Array.isArray(span.raw.links) ? (span.raw.links as Record<string, unknown>[]) : [];
  const kind = KINDS[Number(span.raw.kind)] ?? "";
  const at = (ns: number) => new Date(ns / 1e6).toISOString().slice(11, 23);
  const tabs: [SpanTab, string][] = [["overview", "Overview"], ["attributes", `Attributes ${Object.keys(attrs).length}`], ["resource", "Resource"],
    ["events", `Events & Logs ${span.events.length + logs.length}`], ["links", `Links ${links.length}`]];
  return (
    <aside className="panel span-panel" aria-label={`Span ${span.name}`}>
      <div className="span-panel-head">
        <span className="faint mono">Span #{span.span_id.slice(0, 12)}</span>
        <div className="span-panel-title"><b>{span.name}</b>{kind && kind !== "Unspecified" && <span className="trace-kind">{kind.toUpperCase()}</span>}</div>
        <span className="faint">{span.service}{isError(span.status) && <span className="bad"> · error{span.raw.status_message ? `: ${String(span.raw.status_message)}` : ""}</span>}</span>
      </div>
      <Tabs tabs={tabs} active={tab} onPick={setTab} />
      <div className="span-panel-body">
        {tab === "overview" && (
          <>
            <div className="span-when mono">
              <span>{at(span.start)}</span><b>{fmtMs(span.dur)}</b><span>{at(span.start + span.dur)}</span>
            </div>
            <DurationCompare span={span} />
            <KV entries={Object.entries(attrs).slice(0, 12)} title="Attributes" />
          </>
        )}
        {tab === "attributes" && <KV entries={Object.entries(attrs)} />}
        {tab === "resource" && <KV entries={Object.entries(resource)} />}
        {tab === "events" && (
          <>
            {span.events.length === 0 && logs.length === 0 && <div className="faint">No events or logs for this span.</div>}
            {span.events.map((e, i) => {
              const { "exception.stacktrace": stack, ...rest } = e.attributes;
              return (
                <div key={i} className={`span-event${isException(e) ? " exc" : ""}`}>
                  <div className="span-event-head"><span className="mono faint">+{fmtMs(e.at - span.start)}</span>
                    <b>{isException(e) ? rest["exception.type"] ?? "exception" : e.name}</b>{isException(e) && rest["exception.message"] && <span>{rest["exception.message"]}</span>}</div>
                  <KV entries={Object.entries(rest)} />
                  {stack && <pre className="stack">{stack}</pre>}
                </div>
              );
            })}
            {logs.map((l, i) => (
              <div key={"l" + i} className="span-event">
                <div className="span-event-head"><span className={`sev ${String(l.severity_text ?? "").toLowerCase()}`}>{String(l.severity_text ?? "LOG")}</span><span>{String(l.body ?? "")}</span></div>
              </div>
            ))}
          </>
        )}
        {tab === "links" && (links.length ? links.map((l, i) => <div key={i} className="mono" style={{ fontSize: 12 }}>trace {String(l.trace_id)} · span {String(l.span_id)}</div>) : <div className="faint">No links.</div>)}
      </div>
    </aside>
  );
}

export function KV({ entries, title }: { entries: [string, unknown][]; title?: string }) {
  const shown = entries.filter(([, v]) => v != null && v !== "");
  if (!shown.length) return title ? null : <div className="faint">None.</div>;
  return (
    <div className="span-kv">
      {title && <div className="span-sec">{title}</div>}
      <div className="kv">{shown.map(([k, v]) => <div key={k} style={{ display: "contents" }}><span className="k">{k}</span><span className="v">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span></div>)}</div>
    </div>
  );
}

/** This span's duration against the same operation's spans within half an hour either side. */
export function DurationCompare({ span, noun = "span" }: { span: Span; noun?: string }) {
  const w = { start: new Date(span.start / 1e6 - 1_800_000).toISOString(), end: new Date(span.start / 1e6 + 1_800_000).toISOString() };
  const base = { signal: "traces" as const, ...w, services: [span.service], where: [{ field: "name", op: "=", value: span.name }] };
  const k = span.span_id;
  const hist = useQuery({ ...base, group_by: ["log:duration_ns"], aggs: [{ fn: "count" }], limit: 500 }, "dh" + k);
  const pct = useQuery({ ...base, aggs: ["p50", "p75", "p90", "p99"].map((fn) => ({ fn, field: "duration_ns" })) }, "dp" + k);
  const bars = (hist.data ? records(hist.data) : []).filter((r) => r["log:duration_ns"] != null)
    .map((r) => ({ b: Number(r["log:duration_ns"]), n: Number(r.count) })).sort((a, b) => a.b - b.b);
  const total = bars.reduce((a, x) => a + x.n, 0);
  const mine = Math.floor(Math.log2(Math.max(span.dur, 1)) * LOG_STEPS);   // its log:duration_ns bucket (see bucketLow)
  const p = pct.data ? records(pct.data)[0] ?? {} : {};
  const lo = bars.length ? bars[0].b : 0, hi = bars.length ? bars[bars.length - 1].b + 1 : 1;
  const xOf = (b: number) => ((Math.min(Math.max(b, lo), hi) - lo) / (hi - lo || 1)) * 100;
  // Share of the operation's spans in lower buckets (faster), and in this span's own bucket.
  const faster = total ? (bars.filter((x) => x.b < mine).reduce((a, x) => a + x.n, 0) / total) * 100 : 0;
  const share = total ? ((bars.find((x) => x.b === mine)?.n ?? 0) / total) * 100 : 0;
  const max = Math.max(1, ...bars.map((x) => x.n));
  return (
    <div className="dur-compare">
      <div className="span-sec">{noun[0].toUpperCase() + noun.slice(1)} duration</div>
      <Loads q={hist} empty={!total} height={70}>
        {() => (
          <>
            <p className="faint" style={{ margin: 0 }}>
              {fmtMs(span.dur)} against {fmtNum(total)} <span className="mono">{span.name}</span> {noun}s within 30 min:{" "}
              {faster >= 50 ? `slower than ${faster.toFixed(0)}%` : `faster than ${(100 - faster - share).toFixed(0)}%`} of them.
            </p>
            <div className="dur-chart">
              {bars.map((x) => (
                <span key={x.b} className={`dur-bar${x.b === mine ? " mine" : ""}`}
                      style={{ left: `${xOf(x.b)}%`, width: `${100 / (hi - lo || 1)}%`, height: `${Math.max(6, (x.n / max) * 100)}%` }}
                      title={`${fmtMs(bucketLow(x.b))}–${fmtMs(bucketLow(x.b + 1))}: ${x.n}`} />
              ))}
              <span className="dur-mine" style={{ left: `${xOf(mine)}%` }} title={`this ${noun}: ${fmtMs(span.dur)}`} />
            </div>
            <div className="dur-marks mono">
              {(["p50", "p75", "p90", "p99"] as const).filter((q) => p[`${q}(duration_ns)`] != null).map((q) => (
                <span key={q}><span className="faint">{q === "p50" ? "median" : q}</span> {fmtMs(Number(p[`${q}(duration_ns)`]))}</span>
              ))}
            </div>
          </>
        )}
      </Loads>
    </div>
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
