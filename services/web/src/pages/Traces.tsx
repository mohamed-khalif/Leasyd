import { FormEvent, useMemo, useState } from "react";
import { records } from "../api";
import type { Ctx } from "../App";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { fmtMs, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { fmtTs } from "./Logs";

const SERVICE_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
const LOOKBACK_DAYS = 30;   // trace ids are looked up across 30 days (day/hour ID filters make this fast)

export function Traces({ ctx, traceId }: { ctx: Ctx; traceId?: string }) {
  const [id, setId] = useState(traceId ?? "");
  const submit = (e: FormEvent) => { e.preventDefault(); if (id.trim()) ctx.go(`/traces/${id.trim().toLowerCase()}`); };
  return (
    <>
      <form className="toolbar" onSubmit={submit}>
        <input className="input grow mono" placeholder="Trace ID (32 hex characters)" value={id} onChange={(e) => setId(e.target.value)} aria-label="Trace ID" />
        <button className="btn primary">Open trace</button>
      </form>
      {traceId ? <Waterfall ctx={ctx} traceId={traceId} /> : <Recent ctx={ctx} />}
    </>
  );
}

function Recent({ ctx }: { ctx: Ctx }) {
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const q = useQuery({ signal: "traces", ...w, where: [{ field: "duration_ns", op: ">", value: 0 }], search: { limit: 100 } },
                     "recent" + ctx.range.key + ctx.tick);
  const rows = q.data ? records(q.data).sort((a, b) => Number(b.duration_ns) - Number(a.duration_ns)).slice(0, 50) : [];
  return (
    <Panel title="Slowest recent spans" flush right={<span className="faint">click to open the trace</span>}>
      <Loads q={q} empty={!rows.length} height={200}>
        {() => <RankTable head={["time", "service", "operation", "trace", "duration"]} maxHeight={560}
                          rows={rows.map((r) => [fmtTs(String(r.ts)), String(r.service), String(r.name ?? ""), String(r.trace_id ?? "").slice(0, 16) + "…", fmtMs(Number(r.duration_ns))])}
                          onRow={(i) => ctx.go(`/traces/${rows[i].trace_id}`)} />}
      </Loads>
    </Panel>
  );
}

type Span = { span_id: string; parent_span_id?: string; name: string; service: string; start: number; dur: number; status?: string; depth: number; raw: Record<string, unknown> };

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
             right={tree.length > 0 && <span className="faint mono">{tree.length} spans · {services.length} services · {fmtMs(t1 - t0)}</span>}>
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
                      <span className="wf-dur" style={left + width > 80 ? { right: `${100 - left + 0.5}%` } : { left: `calc(${left + width}% + 6px)` }}>{fmtMs(s.dur)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Loads>
      </Panel>
      {selected && (
        <Panel title={`Span ${selected.name}`} flush>
          <div className="logdetail" style={{ background: "transparent" }}>
            <div className="kv">
              {Object.entries(flatten(selected.raw)).filter(([, v]) => v != null && v !== "").map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}><span className="k">{k}</span><span className="v">{String(v)}</span></div>
              ))}
            </div>
          </div>
        </Panel>
      )}
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

/** OTLP span status: stored as its number (2 = error); older data may hold the enum name. */
export const isError = (status?: string) => status === "2" || status === "STATUS_CODE_ERROR";

/** Spans in tree order (parents before children, siblings by start time) with their depth. */
function layout(rows: Record<string, unknown>[]): Span[] {
  const spans: Span[] = rows.map((r) => ({
    span_id: String(r.span_id), parent_span_id: r.parent_span_id ? String(r.parent_span_id) : undefined,
    name: String(r.name ?? ""), service: String(r.service ?? "unknown"),
    start: Number(r.ts_unix_nano), dur: Number(r.duration_ns ?? 0), status: r.status_code ? String(r.status_code) : undefined,
    depth: 0, raw: r,
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
