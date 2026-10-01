// SQL: one read-only SELECT (DuckDB SQL) over the tables logs, spans and metrics, for the time
// range at the top. Results as a table, or a chart when the first column is a time.
import { KeyboardEvent, useMemo, useState } from "react";
import { sql as runSql, SqlResult } from "../api";
import type { Ctx } from "../App";
import { TimeSeries } from "../components/TimeSeries";
import { fmtNum, rangeWindow } from "../time";

const TABLES: [string, string, [string, string][]][] = [
  ["logs", "One row per log record", [["ts", "timestamp"], ["service", "text"], ["severity_number", "int"], ["severity_text", "text"], ["body", "text"],
    ["trace_id", "text"], ["span_id", "text"], ["scope_name", "text"], ["attributes", "map"], ["resource_attributes", "map"], ["observed_ts", "timestamp"]]],
  ["spans", "One row per span", [["ts", "timestamp"], ["end_ts", "timestamp"], ["duration_ns", "bigint"], ["service", "text"], ["name", "text"],
    ["kind", "int (2 server, 3 client)"], ["status_code", "int (2 error)"], ["status_message", "text"], ["trace_id", "text"], ["span_id", "text"],
    ["parent_span_id", "text"], ["attributes", "map"], ["resource_attributes", "map"], ["events", "list"], ["links", "list"]]],
  ["metrics", "One row per data point", [["ts", "timestamp"], ["service", "text"], ["metric_name", "text"], ["metric_type", "text"], ["unit", "text"],
    ["value", "double"], ["count", "double"], ["sum", "double"], ["min", "double"], ["max", "double"], ["temporality", "int"], ["is_monotonic", "bool"],
    ["bucket_counts", "list"], ["explicit_bounds", "list"], ["attributes", "map"], ["resource_attributes", "map"]]],
];
const EXAMPLES: [string, string][] = [
  ["Slowest endpoints", `SELECT service, name, count(*) AS spans,
       round(quantile_cont(duration_ns, 0.95) / 1e6, 1) AS p95_ms
FROM spans
WHERE kind = 2
GROUP BY ALL
ORDER BY p95_ms DESC
LIMIT 20`],
  ["Errors per minute by service", `SELECT time_bucket(INTERVAL 1 minute, ts) AS minute, service, count(*) AS errors
FROM logs
WHERE severity_number >= 17
GROUP BY ALL
ORDER BY minute`],
  ["Most common error messages", `SELECT service, regexp_replace(body, '[0-9a-f]{8,}|\\d+', '*', 'g') AS message, count(*) AS n
FROM logs
WHERE severity_number >= 17
GROUP BY ALL
ORDER BY n DESC
LIMIT 20`],
  ["Error logs with their trace's root span", `SELECT l.ts, l.service, l.body, s.name AS root_span, s.duration_ns / 1e6 AS root_ms
FROM logs l
JOIN spans s ON s.trace_id = l.trace_id AND s.parent_span_id IS NULL
WHERE l.severity_number >= 17
ORDER BY l.ts DESC
LIMIT 50`],
  ["Requests by HTTP route", `SELECT attributes['http.route'] AS route, count(*) AS requests,
       round(avg(duration_ns) / 1e6, 1) AS avg_ms
FROM spans
WHERE attributes['http.route'] IS NOT NULL
GROUP BY ALL
ORDER BY requests DESC`],
  ["Metric series and points", `SELECT metric_name, count(DISTINCT attributes) AS series, count(*) AS points
FROM metrics
GROUP BY ALL
ORDER BY points DESC`],
];

export function Sql({ ctx }: { ctx: Ctx }) {
  const [text, setText] = useState(() => { try { return localStorage.getItem("leasyd.sql") ?? EXAMPLES[0][1]; } catch { return EXAMPLES[0][1]; } });
  const [result, setResult] = useState<SqlResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [waited, setWaited] = useState(0);   // seconds, once the query runs on in the background
  const [view, setView] = useState<"table" | "chart">("table");
  const [ms, setMs] = useState(0);

  const run = async () => {
    setRunning(true); setError(null); setWaited(0);
    try { localStorage.setItem("leasyd.sql", text); } catch { /* ignore */ }
    const t0 = performance.now();
    try {
      const w = rangeWindow(ctx.range);
      // A day or more of data: run in the background from the start (it may take longer than 20 s).
      const long = Date.parse(w.end) - Date.parse(w.start) >= 86_400_000;
      const r = await runSql({ sql: text, start: w.start, end: w.end }, { background: long, onWait: setWaited });
      setResult(r); setMs(performance.now() - t0);
    } catch (e) { setError((e as Error).message); setResult(null); }
    finally { setRunning(false); }
  };
  const chartable = useMemo(() => chartOf(result), [result]);

  return (
    <>
      <div className="page-head">
        <h1>SQL</h1>
        <span className="faint">One read-only SELECT over <code>logs</code>, <code>spans</code> and <code>metrics</code> for {ctx.range.label.toLowerCase()} (DuckDB SQL)</span>
      </div>
      <div className="sql">
        <aside className="panel sql-schema">
          <div className="views-title" style={{ padding: "10px 12px 4px" }}>Tables</div>
          {TABLES.map(([t, desc, cols]) => (
            <details key={t} open={t === "spans"}>
              <summary><b>{t}</b> <span className="faint">{desc}</span></summary>
              {cols.map(([c, type]) => (
                <button key={c} type="button" className="sql-col" title="Insert" onClick={() => setText((x) => x + (x.endsWith(" ") || x.endsWith("\n") ? "" : " ") + c)}>
                  <span className="mono">{c}</span><span className="faint">{type}</span>
                </button>))}
            </details>
          ))}
          <div className="faint" style={{ padding: "8px 12px", fontSize: 12 }}>
            Attributes: <code>attributes['http.route']</code>. Times are UTC. At most 10,000 rows back and 1 GB read per query.
          </div>
        </aside>
        <div className="sql-main">
          <section className="panel">
            <div className="panel-body">
              <textarea className="input mono qb-editor" style={{ minHeight: 180 }} spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
                        onKeyDown={(e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(); } }} />
              <div className="qb-editor-bar">
                <select className="select" value="" onChange={(e) => e.target.value && setText(e.target.value)} aria-label="Examples">
                  <option value="">Examples…</option>
                  {EXAMPLES.map(([l, x]) => <option key={l} value={x}>{l}</option>)}
                </select>
                <span className="spacer" style={{ flex: 1 }} />
                <button className="btn primary" disabled={running || !text.trim()} onClick={run}>{running ? (waited >= 4 ? `Running… ${waited} s` : "Running…") : <>Run <span className="kbd">Ctrl ↵</span></>}</button>
              </div>
            </div>
          </section>
          {error && <div className="result-box fail" role="alert">{error}</div>}
          {result && (
            <section className="panel">
              <div className="tabs">
                <button type="button" className={view === "table" ? "on" : undefined} onClick={() => setView("table")}>Table</button>
                <button type="button" className={view === "chart" ? "on" : undefined} disabled={!chartable} title={chartable ? undefined : "Chart: first column a time, then numbers (and optionally one text column to split by)"}
                        onClick={() => setView("chart")}>Chart</button>
                <span className="spacer" />
                <span className="faint" style={{ fontSize: 12 }}>{fmtNum(result.rows.length)} rows{result.truncated ? " (first 10,000)" : ""} · {(ms / 1000).toFixed(1)} s
                  {result.stats?.files != null ? ` · ${result.stats.files} files` : ""}</span>
                <button type="button" className="btn small" style={{ marginLeft: 8 }} onClick={() => download(result)}>CSV</button>
              </div>
              {view === "chart" && chartable ? (
                <div className="panel-body"><TimeSeries series={chartable} range={ctx.range} height={280} area={false} /></div>
              ) : !result.rows.length ? <div className="state">No rows</div> : (
                <div className="table-scroll" style={{ maxHeight: 620 }}>
                  <table className="dtable sql-result">
                    <thead><tr>{result.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                    <tbody>{result.rows.slice(0, 2000).map((r, i) => (
                      <tr key={i} style={{ cursor: "default" }}>{r.map((v, j) => <td key={j} title={cell(v)}>{cell(v)}</td>)}</tr>))}</tbody>
                  </table>
                  {result.rows.length > 2000 && <div className="state" style={{ minHeight: 40 }}>Showing 2,000 of {fmtNum(result.rows.length)} rows; download CSV for all.</div>}
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </>
  );
}

const cell = (v: unknown) => (v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));

const COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
/** time, [text], number... -> chart lines (one per number column, or per text value). */
function chartOf(r: SqlResult | null) {
  if (!r || !r.rows.length || r.columns.length < 2) return null;
  const t0 = r.rows[0][0];
  if (typeof t0 !== "string" || isNaN(Date.parse(t0))) return null;
  const split = typeof r.rows[0][1] === "string" && r.columns.length >= 3 ? 1 : -1;
  const nums = r.columns.map((_, j) => j).filter((j) => j > 0 && j !== split && r.rows.every((row) => row[j] == null || typeof row[j] === "number"));
  if (!nums.length) return null;
  const lines = new Map<string, [number, number][]>();
  for (const row of r.rows) {
    for (const j of nums) {
      const name = split >= 0 ? `${row[split]}${nums.length > 1 ? ` · ${r.columns[j]}` : ""}` : r.columns[j];
      if (row[j] == null) continue;
      if (!lines.has(name)) lines.set(name, []);
      lines.get(name)!.push([Date.parse(String(row[0])), Number(row[j])]);
    }
  }
  return [...lines].slice(0, 20).map(([label, points], i) => ({ label, color: COLORS[i % COLORS.length], points }));
}

function download(r: SqlResult) {
  const esc = (v: unknown) => { const s = cell(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const csv = [r.columns.map(esc).join(","), ...r.rows.map((row) => row.map(esc).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "leasyd-query.csv";
  a.click();
}
