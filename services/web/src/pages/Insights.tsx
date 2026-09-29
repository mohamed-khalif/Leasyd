import { useMemo } from "react";
import { Query, records, Signal } from "../api";
import type { Ctx } from "../App";
import { Donut } from "../components/Donut";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { Stat } from "../components/Stat";
import { Series, TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtMs, fmtNum, Range, rangeWindow } from "../time";
import { useQuery } from "../useQuery";

export const SEVERITY_COLORS: Record<string, string> = {
  FATAL: "var(--sev-error)", ERROR: "var(--sev-error)", WARN: "var(--sev-warn)", WARNING: "var(--sev-warn)",
  INFO: "var(--sev-info)", DEBUG: "var(--sev-debug)", TRACE: "var(--sev-debug)",
};
const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];

/** One query per panel, re-run when the time range or refresh tick changes. */
function usePanel(ctx: Ctx, signal: Signal, extra: Omit<Query, "signal" | "start" | "end">) {
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const q: Query = { signal, ...w, ...extra };
  return useQuery(q, JSON.stringify(extra) + signal + ctx.range.key + ctx.tick);
}

/** Group rows of a (time bucket, key, value) result into chart series, top N keys by total. */
export function toSeries(rows: Record<string, unknown>[], key: string, top = 6, colors: Record<string, string> = {}): Series[] {
  const totals = new Map<string, number>();
  for (const r of rows) totals.set(String(r[key] ?? "unknown"), (totals.get(String(r[key] ?? "unknown")) ?? 0) + Number(r.count));
  const keys = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, top).map(([k]) => k);
  return keys.map((k, i) => ({
    label: k, color: colors[k.toUpperCase()] ?? SERIES[i % SERIES.length],
    points: rows.filter((r) => String(r[key] ?? "unknown") === k)
      .map((r) => [Date.parse(String(r["ts:" + bucketOf(rows)] ?? r.bucket)), Number(r.count)] as [number, number]),
  }));
}
function bucketOf(rows: Record<string, unknown>[]): string {
  const k = Object.keys(rows[0] ?? {}).find((c) => c.startsWith("ts:"));
  return k ? k.slice(3) : "";
}

function StatPanel(p: { ctx: Ctx; title: string; signal: Signal; where?: Query["where"]; span?: number }) {
  const q = usePanel(p.ctx, p.signal, { where: p.where, aggs: [{ fn: "count" }] });
  const n = Number(q.data?.rows[0]?.[0] ?? 0);
  return (
    <Panel title={p.title} span={p.span ?? 3}>
      <Loads q={q} height={84}>{() => <Stat value={fmtNum(n)} small />}</Loads>
    </Panel>
  );
}

function TopTable(p: { ctx: Ctx; title: string; signal: Signal; by: string[]; head: string[]; span: number; onRow?: (r: Record<string, unknown>) => void }) {
  const q = usePanel(p.ctx, p.signal, { group_by: p.by, aggs: [{ fn: "count" }], limit: 20 });
  const rows = q.data ? records(q.data) : [];
  return (
    <Panel title={p.title} span={p.span} flush>
      <Loads q={q} empty={!rows.length} height={260}>
        {() => <RankTable head={p.head} maxHeight={380}
                          rows={rows.map((r) => [...p.by.map((b) => String(r[b] ?? "—")), fmtNum(Number(r.count))])}
                          onRow={p.onRow && ((i) => p.onRow!(rows[i]))} />}
      </Loads>
    </Panel>
  );
}

function DonutPanel(p: { ctx: Ctx; title: string; signal: Signal; by: string; colors?: Record<string, string>; span: number }) {
  const q = usePanel(p.ctx, p.signal, { group_by: [p.by], aggs: [{ fn: "count" }], limit: 12 });
  const rows = q.data ? records(q.data) : [];
  return (
    <Panel title={p.title} span={p.span}>
      <Loads q={q} empty={!rows.length} height={220}>
        {() => <Donut slices={rows.map((r, i) => {
          const label = String(r[p.by] ?? "unset");
          return { label, value: Number(r.count), color: p.colors?.[label.toUpperCase()] ?? SERIES[i % SERIES.length] };
        })} />}
      </Loads>
    </Panel>
  );
}

function RatePanel(p: { ctx: Ctx; title: string; signal: Signal; by: string; colors?: Record<string, string>; span: number }) {
  const b = bucketSeconds(p.ctx.range);
  const q = usePanel(p.ctx, p.signal, { group_by: [`ts:${b}`, p.by], aggs: [{ fn: "count" }], limit: 10000 });
  const rows = q.data ? records(q.data) : [];
  const perSec = rows.map((r) => ({ ...r, count: Number(r.count) / b }));
  return (
    <Panel title={p.title} span={p.span}>
      <Loads q={q} empty={!rows.length} height={200}>
        {() => <TimeSeries series={toSeries(perSec, p.by, 6, p.colors)} range={p.ctx.range} unit=" /s" />}
      </Loads>
    </Panel>
  );
}

function LatencyTable(p: { ctx: Ctx; span: number }) {
  const q = usePanel(p.ctx, "traces", {
    group_by: ["service"], limit: 20,
    aggs: [{ fn: "count" }, { fn: "p50", field: "duration_ns" }, { fn: "p95", field: "duration_ns" }, { fn: "p99", field: "duration_ns" }],
  });
  const rows = q.data?.rows ?? [];
  return (
    <Panel title="Latency by service" span={p.span} flush>
      <Loads q={q} empty={!rows.length} height={260}>
        {() => (
          <div className="table-scroll" style={{ maxHeight: 380 }}>
            <table className="table compact">
              <thead><tr><th>service</th><th className="num">p50</th><th className="num">p95</th><th className="num">p99</th><th className="num">spans ↓</th></tr></thead>
              <tbody>{rows.map((r, i) => (
                <tr key={i}><td>{String(r[0])}</td>
                  <td className="num">{r[2] == null ? "—" : fmtMs(Number(r[2]))}</td>
                  <td className="num">{r[3] == null ? "—" : fmtMs(Number(r[3]))}</td>
                  <td className="num">{r[4] == null ? "—" : fmtMs(Number(r[4]))}</td>
                  <td className="num">{fmtNum(Number(r[1]))}</td></tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Loads>
    </Panel>
  );
}

export function Insights({ ctx }: { ctx: Ctx }) {
  const logsFor = (svc: string) => ctx.go(`/logs?service=${encodeURIComponent(svc)}`);
  return (
    <>
      <div className="grid">
        <StatPanel ctx={ctx} title="Log records" signal="logs" />
        <StatPanel ctx={ctx} title="Error logs" signal="logs" where={[{ field: "severity_number", op: ">=", value: 17 }]} />
        <StatPanel ctx={ctx} title="Spans" signal="traces" />
        <StatPanel ctx={ctx} title="Metric data points" signal="metrics" />
      </div>

      <h2 className="section-title">Tracing</h2>
      <div className="grid">
        <TopTable ctx={ctx} title="Top 20: Spans by service" signal="traces" by={["service"]} head={["service", "spans"]} span={5} />
        <DonutPanel ctx={ctx} title="Spans by kind" signal="traces" by="kind" span={3} />
        <RatePanel ctx={ctx} title="Span rate by service" signal="traces" by="service" span={4} />
        <TopTable ctx={ctx} title="Top 20: Spans by operation" signal="traces" by={["name", "service"]} head={["operation", "service", "spans"]} span={7} />
        <LatencyTable ctx={ctx} span={5} />
      </div>

      <h2 className="section-title">Logging</h2>
      <div className="grid">
        <TopTable ctx={ctx} title="Top 20: Services by log count" signal="logs" by={["service"]} head={["service", "logs"]} span={5}
                  onRow={(r) => logsFor(String(r.service))} />
        <DonutPanel ctx={ctx} title="Log count by severity" signal="logs" by="severity_text" colors={SEVERITY_COLORS} span={3} />
        <RatePanel ctx={ctx} title="Incoming log rate by severity" signal="logs" by="severity_text" colors={SEVERITY_COLORS} span={4} />
      </div>

      <h2 className="section-title">Metrics</h2>
      <div className="grid">
        <TopTable ctx={ctx} title="Top 20: Metrics by data points" signal="metrics" by={["metric_name", "service"]} head={["metric", "service", "points"]} span={7}
                  onRow={(r) => ctx.go(`/metrics?m=${encodeURIComponent(String(r.metric_name))}&service=${encodeURIComponent(String(r.service))}`)} />
        <RatePanel ctx={ctx} title="Data point rate by service" signal="metrics" by="service" span={5} />
      </div>
    </>
  );
}

export type { Range };
