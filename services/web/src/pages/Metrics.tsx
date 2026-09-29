import { useMemo, useState } from "react";
import { Query, records, Where } from "../api";
import type { Ctx } from "../App";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { Series, TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";

const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];
const TOP = SERIES.length;                     // lines drawn (one colour each); the table lists every group

type Info = { name: string; type: string; temporality: number | null; monotonic: boolean | null; unit: string };
/** How a metric is charted: gauge-like values, a counter's rate, or a histogram's average and rate. */
type Kind = "gauge" | "counter" | "histogram" | "summary";

function kindOf(m: Info): Kind {
  if (m.type === "sum") return m.monotonic ? "counter" : "gauge";   // an up/down counter is a level
  if (m.type === "histogram" || m.type === "exponential_histogram") return "histogram";
  if (m.type === "summary") return "summary";
  return "gauge";
}
const KIND_LABEL: Record<Kind, string> = { gauge: "Gauge", counter: "Counter", histogram: "Histogram", summary: "Summary" };
const typeLabel = (m: Info) => (m.type === "sum" && !m.monotonic ? "Up/down counter" : KIND_LABEL[kindOf(m)]);

/** OpenTelemetry (UCUM) units as people read them. */
function unitLabel(u: string): string {
  if (!u || u === "1" || /^\{.*\}$/.test(u)) return "";
  return ({ By: "B", KiBy: "KiB", MiBy: "MiB", GiBy: "GiB", "By/s": "B/s", Cel: "°C" } as Record<string, string>)[u] ?? u;
}

export function Metrics({ ctx, params }: { ctx: Ctx; params: URLSearchParams }) {
  const name = params.get("m");
  return name ? <MetricDetail ctx={ctx} name={name} params={params} /> : <MetricList ctx={ctx} />;
}

// ------------------------------------------------------------------ list

function MetricList({ ctx }: { ctx: Ctx }) {
  const [filter, setFilter] = useState("");
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const key = ctx.range.key + ctx.tick;
  const list = useQuery({ signal: "metrics", ...w, group_by: ["metric_name", "metric_type", "temporality", "is_monotonic", "unit"],
                          aggs: [{ fn: "count" }], limit: 2000 }, "l" + key);
  const bySvc = useQuery({ signal: "metrics", ...w, group_by: ["metric_name", "service"], aggs: [{ fn: "count" }], limit: 10000 }, "s" + key);

  const services = new Map<string, number>();
  for (const r of bySvc.data ? records(bySvc.data) : []) services.set(String(r.metric_name), (services.get(String(r.metric_name)) ?? 0) + 1);
  const rows = (list.data ? records(list.data) : [])
    .filter((r) => String(r.metric_name).toLowerCase().includes(filter.trim().toLowerCase()))
    .sort((a, b) => String(a.metric_name).localeCompare(String(b.metric_name)));
  return (
    <>
      <div className="toolbar">
        <input className="input grow mono" placeholder="Filter metrics by name…" value={filter} onChange={(e) => setFilter(e.target.value)}
               aria-label="Filter metrics" />
      </div>
      <Panel title="Metrics" flush right={list.data && <span className="faint">{rows.length} metrics · click one to chart it</span>}>
        <Loads q={list} empty={!rows.length} height={240}>
          {() => <RankTable head={["metric", "type", "unit", "services", "data points"]} numCols={2} maxHeight={640}
                            rows={rows.map((r) => [String(r.metric_name), typeLabel(info(r)), unitLabel(String(r.unit ?? "")) || "—",
                                                   fmtNum(services.get(String(r.metric_name)) ?? 0), fmtNum(Number(r.count))])}
                            onRow={(i) => ctx.go(`/metrics?m=${encodeURIComponent(String(rows[i].metric_name))}`)} />}
        </Loads>
      </Panel>
    </>
  );
}

function info(r: Record<string, unknown>): Info {
  return { name: String(r.metric_name), type: String(r.metric_type ?? "gauge"), unit: String(r.unit ?? ""),
           temporality: r.temporality == null ? null : Number(r.temporality), monotonic: r.is_monotonic == null ? null : Boolean(r.is_monotonic) };
}

// ------------------------------------------------------------------ one metric

const GAUGE_AGGS = [["avg", "Average"], ["max", "Max"], ["min", "Min"], ["p95", "p95"]] as const;

function MetricDetail({ ctx, name, params }: { ctx: Ctx; name: string; params: URLSearchParams }) {
  const [service, setService] = useState(params.get("service") ?? "");
  const [split, setSplit] = useState(params.get("by") ?? "service");
  const [agg, setAgg] = useState("avg");
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const key = name + ctx.range.key + ctx.tick;
  const where: Where[] = [{ field: "metric_name", op: "=", value: name }];

  // What the metric is, which services send it, and a few points to find its attributes.
  const meta = useQuery({ signal: "metrics", ...w, where, group_by: ["metric_type", "temporality", "is_monotonic", "unit"],
                          aggs: [{ fn: "count" }], limit: 10 }, "m" + key);
  const svcs = useQuery({ signal: "metrics", ...w, where, group_by: ["service"], aggs: [{ fn: "count" }], limit: 500 }, "s" + key);
  const sample = useQuery({ signal: "metrics", ...w, where, search: { limit: 50 } }, "x" + key);

  const m = meta.data && meta.data.rows.length ? info({ metric_name: name, ...records(meta.data)[0] }) : null;
  const kind = m ? kindOf(m) : null;
  const sampleRows = sample.data ? records(sample.data) : [];
  const description = sampleRows.find((r) => r.description)?.description as string | undefined;
  const splitOptions = useMemo(() => {
    const keys = new Set<string>();
    for (const r of sampleRows) {
      for (const k of Object.keys((r.attributes as Record<string, unknown>) ?? {})) keys.add(`attributes.${k}`);
      for (const k of Object.keys((r.resource_attributes as Record<string, unknown>) ?? {}))
        if (k !== "service.name") keys.add(`resource.${k}`);
    }
    return [...keys].sort();
  }, [sample.data]);   // eslint-disable-line react-hooks/exhaustive-deps

  const unit = m ? unitLabel(m.unit) : "";
  return (
    <>
      <div className="toolbar">
        <a href="#/metrics" className="btn" onClick={(e) => { e.preventDefault(); ctx.go("/metrics"); }}>← All metrics</a>
        <select className="select" value={service} onChange={(e) => setService(e.target.value)} aria-label="Service">
          <option value="">All services</option>
          {(svcs.data ? records(svcs.data) : []).map((r) => <option key={String(r.service)} value={String(r.service)}>{String(r.service)}</option>)}
        </select>
        <label className="faint" htmlFor="split">Split by</label>
        <select id="split" className="select" value={split} onChange={(e) => setSplit(e.target.value)}>
          <option value="">Nothing (one line)</option>
          <option value="service">service</option>
          {splitOptions.map((k) => <option key={k} value={k}>{k.replace(/^attributes\./, "")}</option>)}
        </select>
        {(kind === "gauge") && (
          <div className="chips" role="radiogroup" aria-label="Aggregation">
            {GAUGE_AGGS.map(([k, l]) => (
              <button type="button" key={k} className={`chip${agg === k ? " on" : ""}`} role="radio" aria-checked={agg === k}
                      onClick={() => setAgg(k)}>{l}</button>
            ))}
          </div>
        )}
      </div>

      <Panel title={name} right={m && <span className="faint">{typeLabel(m)}{unit ? ` · ${unit}` : ""}{description ? ` · ${description}` : ""}</span>}>
        {meta.error ? <div className="state error">{meta.error}</div>
          : !meta.data ? <div className="skeleton" style={{ height: 220 }} />
          : !m ? <div className="state" style={{ minHeight: 220 }}>No data points for this metric in this time range</div>
          : <Charts ctx={ctx} m={m} kind={kind!} agg={agg} split={split} service={service} window={w} />}
      </Panel>
    </>
  );
}

type Line = { label: string; points: [number, number][] };

/** The metric's chart(s) and a table of every group, from one query. */
function Charts(p: { ctx: Ctx; m: Info; kind: Kind; agg: string; split: string; service: string; window: { start: string; end: string } }) {
  const b = bucketSeconds(p.ctx.range);
  const aggs: Query["aggs"] =
    p.kind === "counter" ? [{ fn: "increase", field: "value" }]
    : p.kind === "histogram" ? [{ fn: "increase", field: "sum" }, { fn: "increase", field: "count" }]
    : p.kind === "summary" ? [{ fn: "avg", field: "sum" }, { fn: "avg", field: "count" }]
    : [{ fn: p.agg, field: "value" }];
  const q: Query = { signal: "metrics", ...p.window, where: [{ field: "metric_name", op: "=", value: p.m.name }],
                     ...(p.service ? { services: [p.service] } : {}),
                     group_by: [`ts:${b}`, ...(p.split ? [p.split] : [])], aggs, limit: 10000 };
  const res = useQuery(q, JSON.stringify(q) + p.ctx.tick);
  const unit = unitLabel(p.m.unit);

  // [time, group, a0, a1?] rows -> one line per group, for each chart.
  const { primary, rate } = useMemo(() => {
    const prim = new Map<string, Line>(), rt = new Map<string, Line>();
    for (const row of res.data?.rows ?? []) {
      const t = Date.parse(String(row[0])), label = p.split ? String(row[1] ?? "(none)") : p.m.name;
      const [a0, a1] = row.slice(p.split ? 2 : 1).map((v) => (v == null ? null : Number(v)));
      const add = (map: Map<string, Line>, v: number | null) => {
        if (v == null || !isFinite(v)) return;
        if (!map.has(label)) map.set(label, { label, points: [] });
        map.get(label)!.points.push([t, v]);
      };
      if (p.kind === "counter") add(prim, a0 == null ? null : a0 / b);
      else if (p.kind === "histogram" || p.kind === "summary") {
        add(prim, a0 != null && a1 ? a0 / a1 : null);           // average: total of values / how many
        if (p.kind === "histogram") add(rt, a1 == null ? null : a1 / b);
      } else add(prim, a0);
    }
    return { primary: [...prim.values()], rate: [...rt.values()] };
  }, [res.data]);   // eslint-disable-line react-hooks/exhaustive-deps

  // One colour per group across both charts, by the primary chart's ranking.
  const colors = new Map(rank(primary).map((l, i) => [l.label, SERIES[i % SERIES.length]]));
  const title = p.kind === "counter" ? "Rate per second" : p.kind === "gauge" ? GAUGE_AGGS.find(([k]) => k === p.agg)![1] : "Average";
  const primaryUnit = p.kind === "counter" ? (unit ? ` ${unit}/s` : "/s") : unit ? ` ${unit}` : "";
  return (
    <Loads q={res} empty={!primary.length} height={220}>
      {() => (
        <div style={{ display: "grid", gap: 14 }}>
          <Chart title={title} lines={primary} colors={colors} ctx={p.ctx} unit={primaryUnit} />
          {p.kind === "histogram" && <Chart title="Observations per second" lines={rate} colors={colors} ctx={p.ctx} unit="/s" />}
          <GroupTable lines={primary} split={p.split} unit={primaryUnit} />
        </div>
      )}
    </Loads>
  );
}

function Chart(p: { title: string; lines: Line[]; colors: Map<string, string>; ctx: Ctx; unit: string }) {
  const shown = new Set([...p.colors.keys()].slice(0, TOP));
  const series: Series[] = rank(p.lines).filter((l) => shown.has(l.label))
    .map((l) => ({ label: l.label, color: p.colors.get(l.label)!, points: l.points }));
  return (
    <div>
      <div className="faint" style={{ marginBottom: 4 }}>{p.title}{p.lines.length > TOP ? ` · top ${TOP} of ${p.lines.length}` : ""}</div>
      <TimeSeries series={series} range={p.ctx.range} unit={p.unit} height={200} area={false} />
    </div>
  );
}

function GroupTable(p: { lines: Line[]; split: string; unit: string }) {
  const rows = rank(p.lines).map((l) => {
    const pts = [...l.points].sort((a, b) => a[0] - b[0]), vals = pts.map((x) => x[1]);
    return [l.label, fmtNum(vals[vals.length - 1]) + p.unit, fmtNum(Math.max(...vals)) + p.unit,
            fmtNum(vals.reduce((a, v) => a + v, 0) / vals.length) + p.unit];
  });
  return <RankTable head={[p.split ? p.split.replace(/^attributes\./, "") : "series", "latest", "max", "average"]} rows={rows} numCols={3} maxHeight={320} />;
}

/** Lines by their average, largest first. */
function rank(lines: Line[]): Line[] {
  const avg = (l: Line) => l.points.reduce((a, x) => a + x[1], 0) / (l.points.length || 1);
  return [...lines].sort((a, b) => avg(b) - avg(a));
}
