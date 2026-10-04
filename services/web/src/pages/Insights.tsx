// Home: what is running and whether it is healthy, at a glance. Top cards (services, failing
// checks), volumes, then folding sections for tracing, logging, metrics and alerting, each with
// a way into its explorer.
import { useEffect, useMemo, useState } from "react";
import { Check, checks, Query, records, Signal } from "../api";
import type { Ctx } from "../App";
import { Bar, Card, Cell, Heatmap, Section, StackedBars, StackKey } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtMs, fmtNum, Range, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { SEVERITY_RANGES, severityRange } from "./Logs";
import { isError } from "./Traces";

const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
const SUCCESS = "synthetics.check.success";

/** One query, re-run when the time range or refresh tick changes (null: not needed). */
function usePanel(ctx: Ctx, signal: Signal, extra: Omit<Query, "signal" | "start" | "end"> | null) {
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  return useQuery(extra && { signal, ...w, ...extra }, JSON.stringify(extra) + signal + ctx.range.key + ctx.tick);
}

/** (time bucket, key, count) rows -> stacked bars of the top keys, the rest as "other". */
function stack(rows: Record<string, unknown>[], ts: string, key: string, top = 6, colors?: (k: string, i: number) => string) {
  const totals = new Map<string, number>();
  for (const r of rows) totals.set(String(r[key] ?? "unknown"), (totals.get(String(r[key] ?? "unknown")) ?? 0) + Number(r.count));
  const ks = [...totals].sort((a, b) => b[1] - a[1]).slice(0, top).map(([k]) => k);
  const keys: StackKey[] = [...ks.map((k, i) => ({ label: k, color: colors?.(k, i) ?? SERIES[i % SERIES.length] })),
                            ...(totals.size > top ? [{ label: "other", color: "var(--surface-3)" }] : [])];
  const at = new Map<number, number[]>();
  for (const r of rows) {
    const t = Date.parse(String(r[ts]));
    if (!at.has(t)) at.set(t, keys.map(() => 0));
    const i = ks.indexOf(String(r[key] ?? "unknown"));
    at.get(t)![i >= 0 ? i : keys.length - 1] += Number(r.count);
  }
  return { keys, bars: [...at].map(([t, values]) => ({ t, values })) as Bar[] };
}

function useChecks(tick: number) {
  const [list, setList] = useState<Check[] | null>(null);
  useEffect(() => {
    let live = true;
    checks.list().then((r) => live && setList(r.checks), () => live && setList([]));
    return () => { live = false; };
  }, [tick]);
  return list;
}

export function Insights({ ctx }: { ctx: Ctx }) {
  const b = bucketSeconds(ctx.range), ts = `ts:${b}`;
  const services = usePanel(ctx, "traces", { group_by: ["service"], aggs: [{ fn: "count" }], limit: 1000 });
  const operations = usePanel(ctx, "traces", { group_by: ["service", "name"], aggs: [{ fn: "count" }], limit: 10000 });
  const spans = usePanel(ctx, "traces", { aggs: [{ fn: "count" }] });
  const errorSpans = usePanel(ctx, "traces", { where: [{ field: "status_code", op: "=", value: 2 }], aggs: [{ fn: "count" }] });
  const logs = usePanel(ctx, "logs", { aggs: [{ fn: "count" }] });
  const points = usePanel(ctx, "metrics", { aggs: [{ fn: "count" }] });
  const checkList = useChecks(ctx.tick);
  const runs = usePanel(ctx, "traces", { services: ["synthetics"], where: [{ field: "attributes.check.result", op: "exists" }], search: { limit: 1000 } });
  const byCheck = usePanel(ctx, "metrics", { services: ["synthetics"], where: [{ field: "metric_name", op: "=", value: SUCCESS }, { field: "attributes.check.excluded", op: "not_exists" }],
                                             group_by: ["attributes.check.id"], aggs: [{ fn: "avg", field: "value" }, { fn: "count" }], limit: 1000 });

  // Checks: critical = the latest run failed; degraded = passing now but some runs failed in this range.
  const latest = new Map<string, boolean>();
  for (const r of (runs.data ? records(runs.data) : []).sort((a, c) => String(c.ts).localeCompare(String(a.ts)))) {
    const a = r.attributes as Record<string, unknown>, id = String(a["check.id"]);
    if (!latest.has(id)) latest.set(id, a["check.result"] === "pass");
  }
  const uptime = new Map((byCheck.data ? records(byCheck.data) : []).map((r) => [String(r["attributes.check.id"]), Number(r["avg(value)"])]));
  const active = (checkList ?? []).filter((c) => c.enabled);
  const critical = active.filter((c) => latest.get(c.id) === false);
  const degraded = active.filter((c) => latest.get(c.id) === true && (uptime.get(c.id) ?? 1) < 0.999);
  const n = (q: typeof spans) => (q.data ? fmtNum(Number(q.data.rows[0]?.[0] ?? 0)) : "…");
  const perMin = (q: typeof spans) => (q.data ? `${fmtNum(Number(q.data.rows[0]?.[0] ?? 0) / ctx.range.minutes)} per minute` : "");
  const svcCount = services.data?.rows.length ?? 0, opCount = operations.data?.rows.length ?? 0;
  const errPct = spans.data && errorSpans.data && Number(spans.data.rows[0]?.[0]) ? (Number(errorSpans.data.rows[0]?.[0] ?? 0) / Number(spans.data.rows[0][0])) * 100 : null;

  const count = (q: typeof spans) => (q.data ? Number(q.data.rows[0]?.[0] ?? 0) : null);
  const empty = count(spans) === 0 && count(logs) === 0 && count(points) === 0;   // nothing in the time range yet

  return (
    <>
      <div className="page-head"><h1>Home</h1><span className="faint">{ctx.range.label}</span></div>
      {empty && (
        <div className="start-banner">
          <div><b>No data in this time range yet.</b> <span className="muted">Connect a server, an app or AWS Lambda in about five minutes.</span></div>
          <button className="btn primary" onClick={() => ctx.go("/start")}>Get started</button>
        </div>
      )}
      <div className="grid">
        <div className="span-6">
          <div className="panel" style={{ height: "100%" }}>
            <header className="panel-head"><span>Service monitoring</span><span className="spacer" />
              <button className="btn small" onClick={() => ctx.go("/traces")}>View in Traces</button></header>
            <div className="cards" style={{ padding: 12, gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
              <Card label="Services" value={services.data ? fmtNum(svcCount) : "…"} sub="sending traces" onClick={() => ctx.go("/traces")} />
              <Card label="Operations" value={operations.data ? fmtNum(opCount) + (opCount >= 10000 ? "+" : "") : "…"} sub="distinct span names" />
              <Card label="Errors" value={errPct == null ? "…" : `${errPct.toFixed(errPct < 1 ? 2 : 1)}%`} tone={errPct != null && errPct >= 1 ? "bad" : undefined} sub="of spans failed" />
            </div>
          </div>
        </div>
        <div className="span-6">
          <div className="panel" style={{ height: "100%" }}>
            <header className="panel-head"><span>Alerting: synthetic checks</span><span className="spacer" />
              <button className="btn small" onClick={() => ctx.go("/synthetics")}>View in Synthetics</button></header>
            <div className="cards" style={{ padding: 12, gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
              <Card label="Critical" value={checkList && runs.data ? fmtNum(critical.length) : "…"} tone={critical.length ? "bad" : "ok"}
                    sub={critical.length ? critical.map((c) => c.name).slice(0, 2).join(", ") + (critical.length > 2 ? "…" : "") : "latest run failed"}
                    onClick={critical.length === 1 ? () => ctx.go(`/synthetics/${critical[0].id}`) : () => ctx.go("/synthetics")} />
              <Card label="Degraded" value={checkList && runs.data ? fmtNum(degraded.length) : "…"} tone={degraded.length ? "warn" : undefined}
                    sub={degraded.length ? degraded.map((c) => c.name).slice(0, 2).join(", ") + (degraded.length > 2 ? "…" : "") : "failed earlier, passing now"}
                    onClick={degraded.length === 1 ? () => ctx.go(`/synthetics/${degraded[0].id}`) : () => ctx.go("/synthetics")} />
              <Card label="Healthy" value={checkList && runs.data ? fmtNum(active.length - critical.length - degraded.length) : "…"} tone="ok" sub={`of ${active.length} running checks`} />
            </div>
          </div>
        </div>
      </div>

      <Section id="home.volume" title="Volume">
        <div className="cards">
          <Card label="Spans" value={n(spans)} sub={perMin(spans)} onClick={() => ctx.go("/traces")} />
          <Card label="Error spans" value={n(errorSpans)} sub={perMin(errorSpans)} tone={Number(errorSpans.data?.rows[0]?.[0] ?? 0) > 0 ? "bad" : undefined} onClick={() => ctx.go("/traces")} />
          <Card label="Logs" value={n(logs)} sub={perMin(logs)} onClick={() => ctx.go("/logs")} />
          <Card label="Metric data points" value={n(points)} sub={perMin(points)} onClick={() => ctx.go("/metrics")} />
          <Card label="Synthetic check runs" value={runs.data ? fmtNum(runs.data.rows.length) + (runs.data.rows.length >= 1000 ? "+" : "") : "…"}
                sub={checkList ? `${active.length} checks running` : ""} onClick={() => ctx.go("/synthetics")} />
        </div>
      </Section>

      <Tracing ctx={ctx} b={b} ts={ts} />
      <Logging ctx={ctx} b={b} ts={ts} />
      <Metrics ctx={ctx} b={b} ts={ts} />
      <Alerting ctx={ctx} b={b} ts={ts} checkList={checkList} />
    </>
  );
}

function Tracing({ ctx, b, ts }: { ctx: Ctx; b: number; ts: string }) {
  const heat = usePanel(ctx, "traces", { group_by: [ts, "log:duration_ns", "status_code"], aggs: [{ fn: "count" }], limit: 10000 });
  const bySvc = usePanel(ctx, "traces", { group_by: [ts, "service"], aggs: [{ fn: "count" }], limit: 10000 });
  const errs = usePanel(ctx, "traces", { where: [{ field: "status_code", op: "=", value: 2 }], group_by: [ts], aggs: [{ fn: "count" }], limit: 10000 });
  const lat = usePanel(ctx, "traces", { group_by: [ts], aggs: [{ fn: "count" }, { fn: "p50", field: "duration_ns" }, { fn: "p95", field: "duration_ns" }, { fn: "p99", field: "duration_ns" }], limit: 10000 });
  const cells: Cell[] = useMemo(() => {
    const m = new Map<string, Cell>();
    for (const r of heat.data ? records(heat.data) : []) {
      if (r["log:duration_ns"] == null) continue;
      const t = Date.parse(String(r[ts])), bk = Number(r["log:duration_ns"]), k = `${t}:${bk}`;
      if (!m.has(k)) m.set(k, { t, b: bk, count: 0, errors: 0 });
      const c = m.get(k)!;
      c.count += Number(r.count);
      if (isError(String(r.status_code))) c.errors += Number(r.count);
    }
    return [...m.values()];
  }, [heat.data, ts]);
  const svc = useMemo(() => (bySvc.data ? stack(records(bySvc.data), ts, "service") : null), [bySvc.data, ts]);
  const latRows = lat.data ? records(lat.data) : [];
  const errAt = new Map((errs.data ? records(errs.data) : []).map((r) => [String(r[ts]), Number(r.count)]));
  const pts = (f: (r: Record<string, unknown>) => number | null) =>
    latRows.map((r) => [Date.parse(String(r[ts])), f(r)] as [number, number | null]).filter((p): p is [number, number] => p[1] != null && isFinite(p[1]));
  const ms = (x: unknown) => (x == null ? null : Number(x) / 1e6);
  return (
    <Section id="home.tracing" title="Tracing" right={<button className="btn small" onClick={() => ctx.go("/traces")}>View in Traces</button>}>
      <Panel title="Span duration (outliers in red)">
        <Loads q={heat} empty={!cells.length} height={180}>{() => <Heatmap cells={cells} range={ctx.range} bucketMs={b * 1000} height={180} onCell={() => ctx.go("/traces")} />}</Loads>
      </Panel>
      <div className="grid">
        <Panel title="Spans by service" span={4}>
          <Loads q={bySvc} empty={!svc?.bars.length} height={180}>{() => <StackedBars bars={svc!.bars} keys={svc!.keys} range={ctx.range} bucketMs={b * 1000} height={150} />}</Loads>
        </Panel>
        <Panel title="Error percentage" span={4}>
          <Loads q={lat} empty={!latRows.length} height={180}>
            {() => <TimeSeries series={[{ label: "% of spans failed", color: "var(--sev-error)", points: pts((r) => (Number(r.count) ? ((errAt.get(String(r[ts])) ?? 0) / Number(r.count)) * 100 : null)) }]}
                               range={ctx.range} unit="%" height={150} />}
          </Loads>
        </Panel>
        <Panel title="Duration percentiles" span={4}>
          <Loads q={lat} empty={!latRows.length} height={180}>
            {() => <TimeSeries area={false} range={ctx.range} unit=" ms" height={150}
                               series={[{ label: "p50", color: "var(--series-1)", points: pts((r) => ms(r["p50(duration_ns)"])) },
                                        { label: "p95", color: "var(--series-3)", points: pts((r) => ms(r["p95(duration_ns)"])) },
                                        { label: "p99", color: "var(--series-5)", points: pts((r) => ms(r["p99(duration_ns)"])) }]} />}
          </Loads>
        </Panel>
      </div>
      <LatencyTable ctx={ctx} />
    </Section>
  );
}

function LatencyTable({ ctx }: { ctx: Ctx }) {
  const q = usePanel(ctx, "traces", {
    group_by: ["service"], limit: 20,
    aggs: [{ fn: "count" }, { fn: "p50", field: "duration_ns" }, { fn: "p95", field: "duration_ns" }, { fn: "p99", field: "duration_ns" }],
  });
  const rows = q.data?.rows ?? [];
  const d = (v: unknown) => (v == null ? "—" : fmtMs(Number(v)));
  return (
    <Panel title="Services" flush>
      <Loads q={q} empty={!rows.length} height={160}>
        {() => <RankTable head={["service", "p50", "p95", "p99", "spans"]} numCols={4} maxHeight={320}
                          rows={rows.map((r) => [String(r[0]), d(r[2]), d(r[3]), d(r[4]), fmtNum(Number(r[1]))])}
                          onRow={(i) => ctx.go(`/logs?service=${encodeURIComponent(String(rows[i][0]))}`)} />}
      </Loads>
    </Panel>
  );
}

function Logging({ ctx, b, ts }: { ctx: Ctx; b: number; ts: string }) {
  const hist = usePanel(ctx, "logs", { group_by: [ts, "severity_number"], aggs: [{ fn: "count" }], limit: 10000 });
  const bySvc = usePanel(ctx, "logs", { group_by: ["service"], aggs: [{ fn: "count" }], limit: 20 });
  const errSvc = usePanel(ctx, "logs", { where: [{ field: "severity_number", op: ">=", value: 17 }], group_by: ["service"], aggs: [{ fn: "count" }], limit: 100 });
  const bars = useMemo(() => {
    const at = new Map<number, number[]>();
    for (const r of hist.data ? records(hist.data) : []) {
      const t = Date.parse(String(r[ts]));
      if (!at.has(t)) at.set(t, SEVERITY_RANGES.map(() => 0));
      at.get(t)![severityRange(r.severity_number)] += Number(r.count);
    }
    return [...at].map(([t, values]) => ({ t, values }));
  }, [hist.data, ts]);
  const errs = new Map((errSvc.data ? records(errSvc.data) : []).map((r) => [String(r.service), Number(r.count)]));
  const svcRows = bySvc.data ? records(bySvc.data) : [];
  return (
    <Section id="home.logging" title="Logging" right={<button className="btn small" onClick={() => ctx.go("/logs")}>View in Logs</button>}>
      <div className="grid">
        <Panel title="Log count by severity" span={8}>
          <Loads q={hist} empty={!bars.length} height={200}>
            {() => <StackedBars bars={bars} keys={SEVERITY_RANGES} range={ctx.range} bucketMs={b * 1000} height={180} onBar={() => ctx.go("/logs")} />}
          </Loads>
        </Panel>
        <Panel title="Services by log count" span={4} flush>
          <Loads q={bySvc} empty={!svcRows.length} height={200}>
            {() => <RankTable head={["service", "error & fatal", "logs"]} numCols={2} maxHeight={250}
                              rows={svcRows.map((r) => [String(r.service), errs.get(String(r.service)) ? fmtNum(errs.get(String(r.service))!) : "—", fmtNum(Number(r.count))])}
                              onRow={(i) => ctx.go(`/logs?service=${encodeURIComponent(String(svcRows[i].service))}`)} />}
          </Loads>
        </Panel>
      </div>
    </Section>
  );
}

function Metrics({ ctx, b, ts }: { ctx: Ctx; b: number; ts: string }) {
  const top = usePanel(ctx, "metrics", { group_by: ["metric_name"], aggs: [{ fn: "count" }], limit: 20 });
  const rate = usePanel(ctx, "metrics", { group_by: [ts, "service"], aggs: [{ fn: "count" }], limit: 10000 });
  const svc = useMemo(() => (rate.data ? stack(records(rate.data), ts, "service") : null), [rate.data, ts]);
  const rows = top.data ? records(top.data) : [];
  return (
    <Section id="home.metrics" title="Metrics" right={<button className="btn small" onClick={() => ctx.go("/metrics")}>View in Metrics</button>}>
      <div className="grid">
        <Panel title="Data points by service" span={7}>
          <Loads q={rate} empty={!svc?.bars.length} height={200}>{() => <StackedBars bars={svc!.bars} keys={svc!.keys} range={ctx.range} bucketMs={b * 1000} height={180} />}</Loads>
        </Panel>
        <Panel title="Top metrics by data points" span={5} flush>
          <Loads q={top} empty={!rows.length} height={200}>
            {() => <RankTable head={["metric", "data points"]} maxHeight={250} rows={rows.map((r) => [String(r.metric_name), fmtNum(Number(r.count))])}
                              onRow={(i) => ctx.go(`/metrics?m=${encodeURIComponent(String(rows[i].metric_name))}`)} />}
          </Loads>
        </Panel>
      </div>
    </Section>
  );
}

function Alerting({ ctx, b, ts, checkList }: { ctx: Ctx; b: number; ts: string; checkList: Check[] | null }) {
  // Runs per time bucket that passed / failed (value 1 / 0 of synthetics.check.success), maintenance left out.
  const q = usePanel(ctx, "metrics", { services: ["synthetics"], where: [{ field: "metric_name", op: "=", value: SUCCESS }, { field: "attributes.check.excluded", op: "not_exists" }],
                                       group_by: [ts, "value"], aggs: [{ fn: "count" }], limit: 10000 });
  const byCheck = usePanel(ctx, "metrics", { services: ["synthetics"], where: [{ field: "metric_name", op: "=", value: SUCCESS }, { field: "value", op: "=", value: 0 },
                                             { field: "attributes.check.excluded", op: "not_exists" }], group_by: ["attributes.check.id"], aggs: [{ fn: "count" }], limit: 100 });
  const keys: StackKey[] = [{ label: "failed runs", color: "var(--sev-error)" }, { label: "passed runs", color: "var(--ok, #3fb68b)" }];
  const bars = useMemo(() => {
    const at = new Map<number, number[]>();
    for (const r of q.data ? records(q.data) : []) {
      const t = Date.parse(String(r[ts]));
      if (!at.has(t)) at.set(t, [0, 0]);
      at.get(t)![Number(r.value) >= 1 ? 1 : 0] += Number(r.count);
    }
    return [...at].map(([t, values]) => ({ t, values }));
  }, [q.data, ts]);
  const names = new Map((checkList ?? []).map((c) => [c.id, c.name]));
  const failing = byCheck.data ? records(byCheck.data).filter((r) => Number(r.count) > 0) : [];
  return (
    <Section id="home.alerting" title="Alerting" right={<><button className="btn small" onClick={() => ctx.go("/alerts")}>Alert rules</button>
                                                         <button className="btn small" onClick={() => ctx.go("/synthetics")}>View in Synthetics</button></>}>
      <div className="grid">
        <Panel title="Synthetic check runs: failed and passed" span={8}>
          <Loads q={q} empty={!bars.length} height={200}>
            {() => <StackedBars bars={bars} keys={keys} range={ctx.range} bucketMs={b * 1000} height={180} />}
          </Loads>
        </Panel>
        <Panel title="Checks with failed runs" span={4} flush>
          <Loads q={byCheck} empty={!failing.length} height={200}>
            {() => <RankTable head={["check", "failed runs"]} maxHeight={250}
                              rows={failing.map((r) => [names.get(String(r["attributes.check.id"])) ?? String(r["attributes.check.id"]), fmtNum(Number(r.count))])}
                              onRow={(i) => ctx.go(`/synthetics/${failing[i]["attributes.check.id"]}`)} />}
          </Loads>
        </Panel>
      </div>
    </Section>
  );
}

export type { Range };
