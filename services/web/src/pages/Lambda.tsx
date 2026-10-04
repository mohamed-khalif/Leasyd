// AWS Lambda: the tenant's functions, from traces (spans whose resource has cloud.platform =
// aws_lambda, as OpenTelemetry's Lambda layers send) and from CloudWatch (aws.lambda.* metrics, from
// a metric stream; see examples/aws/cloudwatch-metrics.yaml). A function's page shows its requests,
// errors and duration over time (from traces when it has them, else CloudWatch) and its invocations
// (its entry spans); an invocation opens with its duration against the function's others, its
// payload (the aws.lambda.event or faas.event attribute, when recorded), its logs and its trace.
import { useMemo, useState } from "react";
import { records, Result } from "../api";
import type { Ctx } from "../App";
import { StackedBars, Tabs } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtMs, fmtNum, fmtTime, rangeWindow } from "../time";
import { useSql } from "../useQuery";
import { DurationCompare, isError, KV, Span } from "./Traces";

const ENTRY = "(kind IN (2, 5) OR parent_span_id IS NULL)";
const TRACED = "resource_attributes['cloud.platform'] = 'aws_lambda'";
// CloudWatch's per-function series (it also sends each function by alias / version, and account totals)
const PER_FN = "attributes['FunctionName'] IS NOT NULL AND attributes['Resource'] IS NULL AND attributes['ExecutedVersion'] IS NULL";
const CW = "metric_name IN ('aws.lambda.invocations', 'aws.lambda.errors', 'aws.lambda.duration')";
const CW_AGGS = `sum(sum) FILTER (WHERE metric_name = 'aws.lambda.invocations') AS n,
  sum(sum) FILTER (WHERE metric_name = 'aws.lambda.errors') AS e,
  sum(sum) FILTER (WHERE metric_name = 'aws.lambda.duration') / nullif(sum(count) FILTER (WHERE metric_name = 'aws.lambda.duration'), 0) AS ms`;
const q = (s: string) => `'${s.replace(/'/g, "''")}'`;
const rows = (r: Result | null) => (r ? records(r) : []);
const pct = (e: number, n: number) => (n ? (e / n) * 100 : 0);

type Fn = { name: string; n: number; e: number; ms: number; region: string; account: string; traced: boolean; cloudwatch: boolean };

export function Lambda({ ctx, fn }: { ctx: Ctx; fn?: string }) {
  return fn ? <FunctionView ctx={ctx} fn={fn} /> : <Functions ctx={ctx} />;
}

// ------------------------------------------------------------------ all functions

function Functions({ ctx }: { ctx: Ctx }) {
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const key = ctx.range.key + ctx.tick;
  const traced = useSql(`SELECT service, count(*) AS n, count(*) FILTER (WHERE status_code = 2) AS e, avg(duration_ns) / 1e6 AS ms,
      any_value(resource_attributes['cloud.region']) AS region, any_value(resource_attributes['cloud.account.id']) AS account
    FROM spans WHERE ${TRACED} AND ${ENTRY} GROUP BY 1`, w, "t" + key);
  const cw = useSql(`SELECT service, ${CW_AGGS}, any_value(resource_attributes['cloud.region']) AS region,
      any_value(resource_attributes['cloud.account.id']) AS account
    FROM metrics WHERE ${CW} AND ${PER_FN} GROUP BY 1`, w, "c" + key);
  const [filter, setFilter] = useState("");
  const fns = useMemo(() => {
    const by = new Map<string, Fn>();
    for (const r of rows(traced.data)) {
      by.set(String(r.service), { name: String(r.service), n: Number(r.n), e: Number(r.e), ms: Number(r.ms ?? 0),
                                  region: String(r.region ?? ""), account: String(r.account ?? ""), traced: true, cloudwatch: false });
    }
    for (const r of rows(cw.data)) {   // CloudWatch counts every invocation, so it wins where both exist
      const name = String(r.service), had = by.get(name);
      by.set(name, { name, n: Number(r.n ?? 0), e: Number(r.e ?? 0), ms: Number(r.ms ?? had?.ms ?? 0),
                     region: String(r.region ?? had?.region ?? ""), account: String(r.account ?? had?.account ?? ""),
                     traced: !!had, cloudwatch: true });
    }
    return [...by.values()].sort((a, b) => b.n - a.n);
  }, [traced.data, cw.data]);
  const shown = fns.filter((f) => f.name.toLowerCase().includes(filter.toLowerCase()));
  const loading = { data: traced.data && cw.data ? traced.data : null, error: traced.error || cw.error, loading: traced.loading || cw.loading };

  return (
    <div className="lambda">
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <input className="input grow" placeholder="Search functions…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Search functions" />
        <span className="faint">{fns.length} functions</span>
      </div>
      <Panel title="Functions" flush>
        <Loads q={loading} empty={!fns.length} height={160}>
          {() => (
            <table className="dtable">
              <thead><tr><th style={{ width: "34%" }}>Function</th><th>Region</th><th className="num">Invocations</th><th className="num">Errors</th>
                <th className="num">Error rate</th><th className="num">Duration avg</th><th>From</th></tr></thead>
              <tbody>{shown.map((f) => (
                <tr key={f.name} onClick={() => ctx.go(`/lambda/${encodeURIComponent(f.name)}`)}>
                  <td title={f.name}>{f.name}</td>
                  <td>{f.region}</td>
                  <td className="num">{fmtNum(f.n)}</td>
                  <td className={`num${f.e ? " bad" : ""}`}>{fmtNum(f.e)}</td>
                  <td className={`num${pct(f.e, f.n) >= 1 ? " bad" : ""}`}>{pct(f.e, f.n).toFixed(2)}%</td>
                  <td className="num">{fmtMs(f.ms * 1e6)}</td>
                  <td className="faint">{[f.traced && "traces", f.cloudwatch && "CloudWatch"].filter(Boolean).join(", ")}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </Loads>
      </Panel>
      {!fns.length && !traced.loading && !cw.loading && (
        <p className="faint">
          No Lambda functions in this time range. Send their metrics with a CloudWatch metric stream
          (<span className="mono">examples/aws/cloudwatch-metrics.yaml</span>), and their invocations with OpenTelemetry's Lambda layer.
        </p>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ one function

type Invocation = { ts: string; span_id: string; trace_id: string; name: string; dur: number; status: string; request_id: string;
                    cold: string; attrs: Record<string, unknown> };

function FunctionView({ ctx, fn }: { ctx: Ctx; fn: string }) {
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const b = bucketSeconds(ctx.range);
  const key = fn + ctx.range.key + ctx.tick;
  const [search, setSearch] = useState("");
  const [sel, setSel] = useState<Invocation | null>(null);
  const info = useSql(`SELECT count(*) AS spans,
      any_value(resource_attributes['cloud.region']) AS region, any_value(resource_attributes['cloud.account.id']) AS account,
      any_value(resource_attributes['faas.version']) AS version, any_value(resource_attributes['faas.max_memory']) AS memory,
      any_value(resource_attributes['process.runtime.name']) AS runtime, any_value(resource_attributes['telemetry.sdk.language']) AS language
    FROM spans WHERE service = ${q(fn)} AND ${ENTRY}`, w, "i" + key);
  const cwInfo = useSql(`SELECT any_value(resource_attributes['cloud.region']) AS region, any_value(resource_attributes['cloud.account.id']) AS account
    FROM metrics WHERE service = ${q(fn)} AND ${CW}`, w, "ci" + key);
  const traced = Number(rows(info.data)[0]?.spans ?? 0) > 0;
  const known = !!info.data;
  const series = useSql(!known ? null : traced
    ? `SELECT time_bucket(INTERVAL '${b} seconds', ts) AS t, count(*) AS n, count(*) FILTER (WHERE status_code = 2) AS e,
         avg(duration_ns) / 1e6 AS ms, quantile_cont(duration_ns, 0.95) / 1e6 AS p95
       FROM spans WHERE service = ${q(fn)} AND ${ENTRY} GROUP BY 1 ORDER BY 1`
    : `SELECT time_bucket(INTERVAL '${b} seconds', ts) AS t, ${CW_AGGS}
       FROM metrics WHERE service = ${q(fn)} AND ${CW} AND ${PER_FN} GROUP BY 1 ORDER BY 1`, w, "s" + key + known + traced);
  const s = search.trim().replace(/'/g, "''");
  const invocations = useSql(traced ? `SELECT ts, span_id, trace_id, name, duration_ns, status_code,
        coalesce(attributes['faas.invocation_id'], attributes['faas.execution'], span_id) AS request_id,
        attributes['faas.coldstart'] AS cold, attributes
      FROM spans WHERE service = ${q(fn)} AND ${ENTRY}
        ${s ? `AND coalesce(attributes['faas.invocation_id'], attributes['faas.execution'], span_id) ILIKE '%${s}%'` : ""}
      ORDER BY ts DESC LIMIT 100` : null, w, "v" + key + s + traced);

  const a = { ...rows(cwInfo.data)[0], ...Object.fromEntries(Object.entries(rows(info.data)[0] ?? {}).filter(([, v]) => v != null)) };
  const pts = rows(series.data).map((r) => ({ t: Date.parse(String(r.t)), n: Number(r.n ?? 0), e: Number(r.e ?? 0),
                                              ms: Number(r.ms ?? 0), p95: r.p95 == null ? null : Number(r.p95) }));
  const list: Invocation[] = rows(invocations.data).map((r) => ({
    ts: String(r.ts), span_id: String(r.span_id), trace_id: String(r.trace_id), name: String(r.name), dur: Number(r.duration_ns),
    status: String(r.status_code ?? ""), request_id: String(r.request_id), cold: String(r.cold ?? ""),
    attrs: (r.attributes as Record<string, unknown>) ?? {} }));

  return (
    <div className={`lambda${sel ? " with-panel" : ""}`}>
      <div className="lambda-main">
        <div style={{ display: "flex", gap: 8 }}>
          <button className="linkbtn" onClick={() => ctx.go("/lambda")}>← All functions</button>
        </div>
        <div className="lambda-head">
          {([["Name", fn], ["Region", a.region], ["Account ID", a.account], ["Version", a.version],
             ["Memory", a.memory ? `${a.memory} MB` : ""], ["Runtime", a.runtime || a.language]] as [string, unknown][])
            .filter(([, v]) => v).map(([k, v]) => <div key={k}><span className="faint">{k}</span><b className="mono">{String(v)}</b></div>)}
        </div>
        <div className="lambda-charts">
          <Panel title="Requests">
            <Loads q={series} empty={!pts.length} height={150}>
              {() => <StackedBars range={ctx.range} bucketMs={b * 1000}
                                  keys={[{ label: "Errors", color: "var(--sev-error)" }, { label: "Invocations", color: "var(--text-3)" }]}
                                  bars={pts.map((p) => ({ t: p.t, values: [p.e, Math.max(0, p.n - p.e)] }))} />}
            </Loads>
          </Panel>
          <Panel title="Errors">
            <Loads q={series} empty={!pts.length} height={150}>
              {() => <StackedBars range={ctx.range} bucketMs={b * 1000} legend={false}
                                  keys={[{ label: "Errors", color: "var(--sev-error)" }]} bars={pts.map((p) => ({ t: p.t, values: [p.e] }))} />}
            </Loads>
          </Panel>
          <Panel title="Duration">
            <Loads q={series} empty={!pts.length} height={150}>
              {() => <TimeSeries range={ctx.range} height={150} format={(v) => fmtMs(v * 1e6)}
                                 series={[{ label: "average", color: "var(--series-1)", points: pts.map((p) => [p.t, p.ms] as [number, number]) },
                                          ...(traced ? [{ label: "p95", color: "var(--series-3)",
                                                          points: pts.filter((p) => p.p95 != null).map((p) => [p.t, p.p95!] as [number, number]) }] : [])]} />}
            </Loads>
          </Panel>
        </div>
        <h3 className="lambda-h">Invocations</h3>
        {known && !traced ? (
          <p className="faint">
            This function's numbers come from CloudWatch. To list each invocation, with its payload, logs and trace,
            add OpenTelemetry's Lambda layer to the function (or send spans with cloud.platform = aws_lambda and faas.invocation_id).
          </p>
        ) : (
          <>
            <input className="input" placeholder="Search by request ID…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search invocations" />
            <Panel title={`Latest ${list.length}`} flush>
              <Loads q={invocations} empty={!list.length} height={160}>
                {() => (
                  <table className="dtable">
                    <thead><tr><th style={{ width: "42%" }}>Request ID</th><th>Start time</th><th>Status</th><th className="num">Duration</th><th>Cold start</th></tr></thead>
                    <tbody>{list.map((v) => (
                      <tr key={v.span_id} className={sel?.span_id === v.span_id ? "on" : ""} onClick={() => setSel(v)}>
                        <td title={v.request_id}>{v.request_id}</td>
                        <td>{fmtTime(v.ts, ctx.range)}</td>
                        <td><StatusBadge status={v.status} /></td>
                        <td className="num">{fmtMs(v.dur)}</td>
                        <td>{v.cold === "true" ? <span className="svc-badge warning">cold</span> : ""}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                )}
              </Loads>
            </Panel>
          </>
        )}
      </div>
      {sel && <InvocationPanel ctx={ctx} fn={fn} inv={sel} onClose={() => setSel(null)} />}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const err = isError(status);
  return <span className={`svc-badge${err ? " critical" : ""}`}>{err ? "Error" : status === "1" ? "OK" : "Unset"}</span>;
}

// ------------------------------------------------------------------ one invocation

function InvocationPanel({ ctx, fn, inv, onClose }: { ctx: Ctx; fn: string; inv: Invocation; onClose: () => void }) {
  const [tab, setTab] = useState<"overview" | "payload" | "logs">("overview");
  const start = Date.parse(inv.ts);
  const around = { start: new Date(start - 300_000).toISOString(), end: new Date(start + 300_000).toISOString() };
  const logs = useSql(tab === "logs" ? `SELECT ts, severity_text, body FROM logs WHERE trace_id = ${q(inv.trace_id)} ORDER BY ts LIMIT 500` : null,
                      around, "l" + inv.span_id + tab);
  const span: Span = { span_id: inv.span_id, name: inv.name, service: fn, start: start * 1e6, dur: inv.dur, status: inv.status,
                       depth: 0, events: [], raw: {} };
  const payload = inv.attrs["aws.lambda.event"] ?? inv.attrs["faas.event"];
  let pretty = "";
  if (payload != null) {
    try { pretty = JSON.stringify(JSON.parse(String(payload)), null, 2); } catch { pretty = String(payload); }
  }
  const attrs = Object.entries(inv.attrs).filter(([k]) => k !== "aws.lambda.event" && k !== "faas.event").sort(([x], [y]) => x.localeCompare(y));

  return (
    <aside className="svc-panel lambda-panel" aria-label={`Invocation ${inv.request_id}`}>
      <div className="svc-panel-head">
        <div>
          <div className="faint">Invocation</div>
          <div className="svc-panel-title mono">{inv.request_id}</div>
        </div>
        <StatusBadge status={inv.status} />
        <button className="linkbtn" onClick={onClose} aria-label="Close">✕</button>
      </div>
      <Tabs tabs={[["overview", "Overview"], ["payload", "Payload"], ["logs", "Logs"]]} active={tab} onPick={setTab} />
      <div className="svc-panel-body">
        {tab === "overview" && (
          <>
            <KV entries={[["Start", new Date(start).toLocaleString()], ["Duration", fmtMs(inv.dur)],
                          ["Cold start", inv.cold === "true" ? "yes" : inv.cold === "false" ? "no" : ""], ["Trace ID", inv.trace_id]]} />
            {payload != null && <button className="linkbtn" onClick={() => setTab("payload")}>See the payload →</button>}
            <DurationCompare span={span} noun="invocation" />
            <button className="btn" onClick={() => ctx.go(`/traces/${inv.trace_id}`)}>View full trace</button>
            <KV title="Attributes" entries={attrs} />
          </>
        )}
        {tab === "payload" && (pretty ? <pre className="lambda-payload">{pretty}</pre> : (
          <p className="faint">
            No payload recorded for this invocation. Leasyd shows the event when the span has an
            aws.lambda.event (or faas.event) attribute holding it as JSON.
          </p>
        ))}
        {tab === "logs" && (
          <Loads q={logs} empty={!rows(logs.data).length} height={120}>
            {() => (
              <div className="lambda-logs">
                {rows(logs.data).map((r, i) => (
                  <div key={i} className={`lambda-log${/ERROR|FATAL/i.test(String(r.severity_text)) ? " bad" : ""}`}>
                    <span className="faint mono">{new Date(Date.parse(String(r.ts))).toLocaleTimeString()}</span>
                    <span className="mono">{String(r.severity_text ?? "")}</span>
                    <span>{typeof r.body === "object" ? JSON.stringify(r.body) : String(r.body ?? "")}</span>
                  </div>
                ))}
              </div>
            )}
          </Loads>
        )}
      </div>
    </aside>
  );
}
