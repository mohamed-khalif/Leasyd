import { FormEvent, Fragment, useMemo, useState } from "react";
import { Query, records, Where } from "../api";
import type { Ctx } from "../App";
import { Bar, ColumnPicker, StackedBars, StackKey, Tabs, useColumns, View, ViewsSidebar } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { bucketSeconds, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";

// OpenTelemetry severity numbers: 1-4 trace, 5-8 debug, 9-12 info, 13-16 warn, 17-20 error, 21-24 fatal.
export const SEVERITY_RANGES: (StackKey & { min: number; max: number; key: string })[] = [
  { key: "error", label: "ERROR & FATAL", color: "var(--sev-error)", min: 17, max: 24 },
  { key: "warn", label: "WARN", color: "var(--sev-warn)", min: 13, max: 16 },
  { key: "info", label: "INFO", color: "var(--sev-info)", min: 9, max: 12 },
  { key: "debug", label: "TRACE & DEBUG", color: "var(--sev-debug)", min: 1, max: 8 },
  { key: "unknown", label: "UNKNOWN", color: "var(--surface-3)", min: 0, max: 0 },
];
export const severityRange = (n: unknown) => {
  const v = Number(n) || 0;
  const i = SEVERITY_RANGES.findIndex((r) => v >= r.min && v <= r.max);
  return i < 0 ? SEVERITY_RANGES.length - 1 : i;
};

const VIEWS: (View & { where: Where[]; services?: string[] })[] = [
  { key: "all", label: "All logs", where: [] },
  { key: "errors", label: "Errors & fatal", where: [{ field: "severity_number", op: ">=", value: 17 }] },
  { key: "warnings", label: "Warnings", where: [{ field: "severity_number", op: ">=", value: 13 }, { field: "severity_number", op: "<", value: 17 }] },
  { key: "traced", label: "Logs in a trace", hint: "Records that carry a trace id", where: [{ field: "trace_id", op: "exists" }] },
  { key: "synthetics", label: "Synthetic check failures", group: "Leasyd", where: [], services: ["synthetics"] },
  { key: "alerts", label: "Alert history", group: "Leasyd", where: [], services: ["alerts"] },
];
const LEVEL_VIEW: Record<string, string> = { error: "errors", warn: "warnings" };   // old links: ?level=

const GROUPS: [string, string][] = [
  ["", "Nothing"], ["service", "Service"], ["severity_text", "Severity"], ["scope_name", "Scope"],
  ["resource.host.name", "Host"], ["resource.k8s.namespace.name", "Namespace"], ["resource.deployment.environment", "Environment"],
  ["attributes.http.route", "HTTP route"],
];
const GROUP_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
const TOP_GROUPS = 6;

const COLUMNS = [
  { key: "severity", label: "Severity" }, { key: "time", label: "Time" }, { key: "resource", label: "Resource" },
  { key: "body", label: "Body" }, { key: "attributes", label: "Attributes" },
];
const WIDTH: Record<string, number | undefined> = { severity: 90, time: 190, resource: 150, attributes: 260 };
const LIMIT = 200, PATTERN_SAMPLE = 1000;

type Filter = { field: string; value: string };

export function Logs({ ctx, params }: { ctx: Ctx; params: URLSearchParams }) {
  const [text, setText] = useState(params.get("q") ?? "");
  const [applied, setApplied] = useState(params.get("q") ?? "");
  const [view, setView] = useState(params.get("view") ?? LEVEL_VIEW[params.get("level") ?? ""] ?? "all");
  const [service, setService] = useState(params.get("service") ?? "");
  const [filters, setFilters] = useState<Filter[]>([]);
  const [groupBy, setGroupBy] = useState("");
  const [custom, setCustom] = useState("");
  const [tab, setTab] = useState<"table" | "groups" | "patterns">("table");
  const [open, setOpen] = useState<number | null>(null);
  const [shown, setShown] = useColumns("logs", COLUMNS);

  const v = VIEWS.find((x) => x.key === view) ?? VIEWS[0];
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const where: Where[] = [...v.where, ...filters.map((f) => ({ field: f.field, op: "=", value: f.value }))];
  if (applied.trim()) where.push({ field: "body", op: "contains", value: applied.trim() });
  const services = v.services ?? (service ? [service] : undefined);
  const base: Omit<Query, "group_by" | "aggs" | "search" | "limit"> = { signal: "logs", ...w, where, ...(services ? { services } : {}) };
  const key = JSON.stringify(base) + ctx.tick;
  const b = bucketSeconds(ctx.range);
  const g = groupBy === "custom" ? (custom.trim() ? `attributes.${custom.trim()}` : "") : groupBy;

  const hist = useQuery({ ...base, group_by: [`ts:${b}`, "severity_number"], aggs: [{ fn: "count" }], limit: 10000 }, "h" + key);
  const byGroup = useQuery(g ? { ...base, group_by: [`ts:${b}`, g], aggs: [{ fn: "count" }], limit: 10000 } : null, "g" + key + g);
  const groups = useQuery(g && tab === "groups" ? { ...base, group_by: [g], aggs: [{ fn: "count" }], limit: 200 } : null, "G" + key + g + tab);
  const groupErrors = useQuery(g && tab === "groups" ? { ...base, where: [...where, { field: "severity_number", op: ">=", value: 17 }], group_by: [g], aggs: [{ fn: "count" }], limit: 200 } : null,
                               "E" + key + g + tab);
  const rows = useQuery(tab === "table" ? { ...base, search: { limit: LIMIT } } : null, "r" + key + tab);
  const sample = useQuery(tab === "patterns" ? { ...base, search: { limit: PATTERN_SAMPLE } } : null, "p" + key + tab);
  const svcList = useQuery({ signal: "logs", ...w, group_by: ["service"], aggs: [{ fn: "count" }], limit: 100 }, "s" + ctx.range.key + ctx.tick);

  // Counts for the header and the severity chart.
  const { bars, total, errors, warns } = useMemo(() => {
    const at = new Map<number, number[]>();
    let total = 0, errors = 0, warns = 0;
    for (const r of hist.data ? records(hist.data) : []) {
      const t = Date.parse(String(r[`ts:${b}`])), n = Number(r.count), i = severityRange(r.severity_number);
      if (!at.has(t)) at.set(t, SEVERITY_RANGES.map(() => 0));
      at.get(t)![i] += n;
      total += n; if (i === 0) errors += n; if (i === 1) warns += n;
    }
    return { bars: [...at].map(([t, values]) => ({ t, values })), total, errors, warns };
  }, [hist.data, b]);
  const grouped = useMemo(() => (byGroup.data && g ? stackByGroup(records(byGroup.data), `ts:${b}`, g) : null), [byGroup.data, g, b]);

  const reset = () => setOpen(null);
  const submit = (e: FormEvent) => { e.preventDefault(); setApplied(text); reset(); };
  const addFilter = (field: string, value: string) => {
    if (field === "service") setService(value);
    else if (!filters.some((f) => f.field === field && f.value === value)) setFilters([...filters, { field, value }]);
    setTab("table"); reset();
  };
  const lines = rows.data ? records(rows.data) : [];

  return (
    <div className="explorer">
      <ViewsSidebar views={VIEWS} active={view} onPick={(k) => { setView(k); reset(); }} />
      <div className="explorer-main">
        <div className="page-head">
          <h1>{v.label}</h1>
          <span className="head-count"><b>{hist.data ? fmtNum(total) : "…"}</b>records</span>
          <span className="head-count bad"><b>{hist.data ? fmtNum(errors) : "…"}</b>error &amp; fatal</span>
          <span className="head-count warn"><b>{hist.data ? fmtNum(warns) : "…"}</b>warn</span>
        </div>

        <form className="toolbar" onSubmit={submit}>
          <input className="input grow mono" placeholder="Search log messages…" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search log messages" />
          {!v.services && (
            <select className="select" value={service} onChange={(e) => { setService(e.target.value); reset(); }} aria-label="Service">
              <option value="">All services</option>
              {(svcList.data ? records(svcList.data) : []).map((r) => <option key={String(r.service)} value={String(r.service)}>{String(r.service)}</option>)}
            </select>
          )}
          <label className="faint" htmlFor="groupby">Group by</label>
          <select id="groupby" className="select" value={groupBy} onChange={(e) => { setGroupBy(e.target.value); if (e.target.value) setTab("groups"); }}>
            {GROUPS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            <option value="custom">Attribute…</option>
          </select>
          {groupBy === "custom" && <input className="input mono" style={{ width: 170 }} placeholder="attribute key" value={custom} onChange={(e) => setCustom(e.target.value)} aria-label="Attribute key" />}
          <button className="btn primary">Search</button>
        </form>
        {(filters.length > 0 || service) && !v.services && (
          <div className="chips">
            {service && <button type="button" className="chip on" onClick={() => setService("")}>service = {service} ✕</button>}
            {filters.map((f) => (
              <button key={f.field + f.value} type="button" className="chip on" onClick={() => setFilters(filters.filter((x) => x !== f))}>
                {f.field.replace(/^attributes\./, "")} = {f.value} ✕
              </button>
            ))}
          </div>
        )}

        <Panel title={g && grouped ? `Log count by ${label(g)}` : "Log count by severity"}>
          <Loads q={g ? byGroup : hist} empty={!total} height={150}>
            {() => g && grouped
              ? <StackedBars bars={grouped.bars} keys={grouped.keys} range={ctx.range} bucketMs={b * 1000} height={140} />
              : <StackedBars bars={bars} keys={SEVERITY_RANGES} range={ctx.range} bucketMs={b * 1000} height={140} />}
          </Loads>
        </Panel>

        <section className="panel">
          <Tabs tabs={[["table", "Table"], ["groups", "Groups"], ["patterns", "Patterns"]]} active={tab} onPick={(t) => { setTab(t); reset(); }}
                right={tab === "table" ? <ColumnPicker columns={COLUMNS} shown={shown} onChange={setShown} />
                  : tab === "patterns" && sample.data ? <span className="faint">from the newest {fmtNum(sample.data.rows.length)} records</span> : null} />
          {tab === "table" && (
            <Loads q={rows} empty={!lines.length} height={220}>
              {() => (
                <div className="table-scroll" style={{ maxHeight: 640 }}>
                  <table className="dtable">
                    <colgroup>{COLUMNS.filter((c) => shown.includes(c.key)).map((c) => <col key={c.key} style={{ width: WIDTH[c.key] }} />)}</colgroup>
                    <thead><tr>{COLUMNS.filter((c) => shown.includes(c.key)).map((c) => <th key={c.key}>{c.label}</th>)}</tr></thead>
                    <tbody>
                      {lines.map((r, i) => (
                        <Fragment key={i}>
                          <tr className={open === i ? "on" : undefined} onClick={() => setOpen(open === i ? null : i)}>
                            {shown.includes("severity") && <td><SeverityCell r={r} /></td>}
                            {shown.includes("time") && <td className="faint">{fmtTs(String(r.ts))}</td>}
                            {shown.includes("resource") && <td className="muted">{String(r.service)}</td>}
                            {shown.includes("body") && <td>{String(r.body ?? "")}</td>}
                            {shown.includes("attributes") && <td><Attrs a={r.attributes} /></td>}
                          </tr>
                          {open === i && <tr><td colSpan={shown.length} style={{ padding: 0, cursor: "default" }}><Detail r={r} go={ctx.go} onFilter={addFilter} /></td></tr>}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                  {lines.length >= LIMIT && <div className="state" style={{ minHeight: 40 }}>The newest {LIMIT} records. Search or narrow the time range to see others.</div>}
                </div>
              )}
            </Loads>
          )}
          {tab === "groups" && (!g
            ? <div className="state">Choose what to group by above (service, severity, host, or any attribute).</div>
            : <Loads q={groups} empty={!groups.data?.rows.length} height={200}>
                {() => <GroupTable field={g} rows={records(groups.data!)} errors={groupErrors.data ? records(groupErrors.data) : []} onPick={(value) => addFilter(g === "severity_text" ? "severity_text" : g, value)} />}
              </Loads>)}
          {tab === "patterns" && (
            <Loads q={sample} empty={!sample.data?.rows.length} height={200}>
              {() => <PatternTable rows={records(sample.data!)} />}
            </Loads>
          )}
        </section>
      </div>
    </div>
  );
}

const label = (g: string) => GROUPS.find(([k]) => k === g)?.[1].toLowerCase() ?? g.replace(/^attributes\./, "");

/** (time, group, count) rows -> stacked bars of the top groups, the rest as "other". */
function stackByGroup(rows: Record<string, unknown>[], ts: string, g: string) {
  const totals = new Map<string, number>();
  for (const r of rows) totals.set(String(r[g] ?? "(none)"), (totals.get(String(r[g] ?? "(none)")) ?? 0) + Number(r.count));
  const top = [...totals].sort((a, b) => b[1] - a[1]).slice(0, TOP_GROUPS).map(([k]) => k);
  const other = totals.size > TOP_GROUPS;
  const keys: StackKey[] = [...top.map((k, i) => ({ label: k, color: GROUP_COLORS[i] })), ...(other ? [{ label: "other", color: "var(--surface-3)" }] : [])];
  const at = new Map<number, number[]>();
  for (const r of rows) {
    const t = Date.parse(String(r[ts]));
    if (!at.has(t)) at.set(t, keys.map(() => 0));
    const i = top.indexOf(String(r[g] ?? "(none)"));
    at.get(t)![i >= 0 ? i : keys.length - 1] += Number(r.count);
  }
  return { keys, bars: [...at].map(([t, values]) => ({ t, values })) as Bar[] };
}

function GroupTable(p: { field: string; rows: Record<string, unknown>[]; errors: Record<string, unknown>[]; onPick: (v: string) => void }) {
  const total = p.rows.reduce((a, r) => a + Number(r.count), 0) || 1;
  const errs = new Map(p.errors.map((r) => [String(r[p.field] ?? "(none)"), Number(r.count)]));
  const top = Math.max(...p.rows.map((r) => Number(r.count)), 1);
  return (
    <div className="table-scroll" style={{ maxHeight: 640 }}>
      <table className="dtable">
        <colgroup><col /><col style={{ width: 110 }} /><col style={{ width: 110 }} /><col style={{ width: 200 }} /></colgroup>
        <thead><tr><th>{label(p.field)}</th><th className="num">records ↓</th><th className="num">error &amp; fatal</th><th>share</th></tr></thead>
        <tbody>
          {p.rows.map((r) => {
            const k = String(r[p.field] ?? "(none)"), n = Number(r.count), e = errs.get(k) ?? 0;
            return (
              <tr key={k} onClick={() => r[p.field] != null && p.onPick(k)} title={r[p.field] != null ? "Show only these records" : undefined}>
                <td>{k}</td><td className="num">{fmtNum(n)}</td>
                <td className="num" style={e ? { color: "var(--sev-error)" } : undefined}>{e ? fmtNum(e) : "—"}</td>
                <td><span className="share-bar" style={{ width: `${(n / top) * 140}px` }} /> <span className="faint">{((n / total) * 100).toFixed(1)}%</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ patterns

const VARIABLE = [
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi,       // uuids
  /\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b/g,                                     // ip addresses
  /\b[\w.+-]+@[\w-]+\.[\w.]+\b/g,                                               // emails
  /\b(?=[0-9a-f]*\d)[0-9a-f]{6,}\b/gi,                                           // hex ids
  /"[^"]{0,200}"|'[^']{0,200}'/g,                                               // quoted values
  /(?<=[=:]\s?)[^\s,;)}\]]+/g,                                                   // key=value, key: value
  /\b\d+(?:[.,]\d+)*(?:ms|s|m|h|%|b|kb|mb|gb)?\b/gi,                           // numbers, sizes and times
];
/** A log body with its variable parts (ids, numbers, values) replaced by <*>. */
export function pattern(body: string): string {
  let s = body.length > 500 ? body.slice(0, 500) : body;
  for (const re of VARIABLE) s = s.replace(re, "<*>");
  return s.replace(/(<\*>[\s,]*){2,}/g, "<*> ");
}

function PatternTable({ rows }: { rows: Record<string, unknown>[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const pats = useMemo(() => {
    const m = new Map<string, { n: number; sev: number; text: string; services: Set<string>; examples: Record<string, unknown>[] }>();
    for (const r of rows) {
      const p = pattern(String(r.body ?? ""));
      if (!m.has(p)) m.set(p, { n: 0, sev: SEVERITY_RANGES.length - 1, text: "", services: new Set(), examples: [] });
      const x = m.get(p)!, sev = severityRange(r.severity_number);
      if (sev < x.sev || !x.text) { x.sev = Math.min(x.sev, sev); x.text = String(r.severity_text ?? "") || sevName(Number(r.severity_number)).toUpperCase(); }
      x.n++; x.services.add(String(r.service));
      if (x.examples.length < 5) x.examples.push(r);
    }
    return [...m].sort((a, b) => b[1].n - a[1].n);
  }, [rows]);
  return (
    <div className="table-scroll" style={{ maxHeight: 640 }}>
      <table className="dtable">
        <colgroup><col style={{ width: 90 }} /><col /><col style={{ width: 200 }} /><col style={{ width: 100 }} /></colgroup>
        <thead><tr><th>Severity</th><th>Pattern</th><th>Services</th><th className="num">records ↓</th></tr></thead>
        <tbody>
          {pats.map(([p, x]) => (
            <Fragment key={p}>
              <tr className={open === p ? "on" : undefined} onClick={() => setOpen(open === p ? null : p)}>
                <td><span className="sev-pill" style={{ background: SEVERITY_RANGES[x.sev].color }} /><span className="faint">{x.text || "—"}</span></td>
                <td>{p}</td><td className="muted">{[...x.services].join(", ")}</td><td className="num">{fmtNum(x.n)}</td>
              </tr>
              {open === p && (
                <tr><td colSpan={4} style={{ padding: "4px 10px 10px", cursor: "default" }}>
                  <div className="faint" style={{ margin: "4px 0" }}>Examples</div>
                  {x.examples.map((r, i) => <div key={i} className="mono" style={{ whiteSpace: "pre-wrap" }}><span className="faint">{fmtTs(String(r.ts))}</span>  {String(r.body ?? "")}</div>)}
                </td></tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ cells and details

function SeverityCell({ r }: { r: Record<string, unknown> }) {
  const i = severityRange(r.severity_number);
  const text = String(r.severity_text ?? "") || sevName(Number(r.severity_number)).toUpperCase() || "—";
  return <><span className="sev-pill" style={{ background: SEVERITY_RANGES[i].color }} /><span className={`sev ${sevName(Number(r.severity_number))}`}>{text}</span></>;
}

function Attrs({ a }: { a: unknown }) {
  const e = Object.entries((a as Record<string, unknown>) ?? {}).filter(([, v]) => v != null && v !== "");
  return <>{e.slice(0, 4).map(([k, v]) => <span key={k} className="attr-chip">{k}={String(v)}</span>)}{e.length > 4 && <span className="faint">+{e.length - 4}</span>}</>;
}

function Detail({ r, go, onFilter }: { r: Record<string, unknown>; go: (h: string) => void; onFilter?: (field: string, value: string) => void }) {
  const flat: [string, unknown, string?][] = [];   // [label, value, field to filter on]
  for (const [k, v] of Object.entries(r)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const prefix = k === "resource_attributes" ? "resource" : k;
      for (const [k2, v2] of Object.entries(v as Record<string, unknown>)) flat.push([`${prefix}.${k2}`, v2, `${prefix}.${k2}`]);
    } else flat.push([k, v, ["service", "severity_text", "scope_name"].includes(k) ? k : undefined]);
  }
  return (
    <div className="logdetail">
      <div className="kv">
        {flat.filter(([, v]) => v != null && v !== "").map(([k, v, field]) => (
          <div key={k} style={{ display: "contents" }}>
            <span className="k">{k}</span>
            <span className="v">
              {k === "trace_id"
                ? <a href={`#/traces/${v}`} onClick={(e) => { e.preventDefault(); go(`/traces/${v}`); }}>{String(v)}</a>
                : String(v)}
              {field && onFilter && typeof v !== "object" && field !== "resource.service.name" && (
                <button type="button" className="linkbtn" style={{ marginLeft: 8, fontSize: 11 }} onClick={() => onFilter(field, String(v))}>filter</button>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function sevName(n: number): string {
  if (n >= 21) return "fatal"; if (n >= 17) return "error"; if (n >= 13) return "warn";
  if (n >= 9) return "info"; if (n >= 5) return "debug"; return n ? "trace" : "";
}

function fmtTs(iso: string): string {
  const d = new Date(iso);
  if (isNaN(+d)) return iso;
  const p = (n: number, l = 2) => String(n).padStart(l, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}
export { fmtTs };
