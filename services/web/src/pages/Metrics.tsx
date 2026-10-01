import { Fragment, useMemo, useState } from "react";
import { Query, records, Where } from "../api";
import type { Ctx } from "../App";
import { Tabs, Treemap } from "../components/Charts";
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
  const [size, setSize] = useState<"cardinality" | "points">("cardinality");
  const [folded, setFolded] = useState<Record<string, boolean>>({});
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const key = ctx.range.key + ctx.tick;
  const list = useQuery({ signal: "metrics", ...w, group_by: ["metric_name", "metric_type", "temporality", "is_monotonic", "unit"],
                          aggs: [{ fn: "count" }], limit: 2000 }, "l" + key);
  const bySvc = useQuery({ signal: "metrics", ...w, group_by: ["metric_name", "service"], aggs: [{ fn: "count" }], limit: 10000 }, "s" + key);
  // Cardinality: how many series (distinct service + attribute sets) each metric has.
  const card = useQuery({ signal: "metrics", ...w, group_by: ["metric_name", "service", "hash:attributes"], aggs: [{ fn: "count" }],
                          collapse: 1, limit: 2000 }, "c" + key);

  const services = new Map<string, number>();
  for (const r of bySvc.data ? records(bySvc.data) : []) services.set(String(r.metric_name), (services.get(String(r.metric_name)) ?? 0) + 1);
  const series = new Map((card.data ? records(card.data) : []).map((r) => [String(r.metric_name), Number(r.groups)]));
  const rows = (list.data ? records(list.data) : [])
    .filter((r) => String(r.metric_name).toLowerCase().includes(filter.trim().toLowerCase()))
    .sort((a, b) => String(a.metric_name).localeCompare(String(b.metric_name)));
  const prefixes = useMemo(() => {
    const m = new Map<string, Record<string, unknown>[]>();
    for (const r of rows) {
      const p = String(r.metric_name).split(".")[0];
      m.set(p, [...(m.get(p) ?? []), r]);
    }
    return [...m];
  }, [rows]);   // eslint-disable-line react-hooks/exhaustive-deps
  const open = (name: string) => ctx.go(`/metrics?m=${encodeURIComponent(name)}`);
  const collapsedByDefault = rows.length > 30 && !filter.trim();
  const isOpen = (p: string) => folded[p] ?? !collapsedByDefault;
  const cardText = (n?: number) => (n == null ? (card.data ? "—" : "…") : fmtNum(n) + (card.data?.truncated ? "+" : ""));
  const tiles = rows.map((r) => {
    const name = String(r.metric_name), n = size === "cardinality" ? series.get(name) ?? 0 : Number(r.count);
    return { label: name, value: n, sub: size === "cardinality" ? `${fmtNum(Number(r.count))} data points` : `${cardText(series.get(name))} series` };
  });
  return (
    <>
      <div className="page-head">
        <h1>Metrics</h1>
        <span className="head-count"><b>{list.data ? fmtNum(rows.length) : "…"}</b>metrics</span>
        <span className="head-count"><b>{list.data ? fmtNum(rows.reduce((a, r) => a + Number(r.count), 0)) : "…"}</b>data points</span>
        <span className="head-count"><b>{card.data ? fmtNum([...series.values()].reduce((a, n) => a + n, 0)) + (card.data.truncated ? "+" : "") : "…"}</b>series</span>
      </div>
      <div className="toolbar">
        <input className="input grow mono" placeholder="Filter metrics by name…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter metrics" />
      </div>
      <section className="panel">
        <Tabs tabs={[["cardinality", "By cardinality"], ["points", "By data points"]]} active={size} onPick={setSize}
              right={<span className="faint" style={{ fontSize: 12 }}>{size === "cardinality" ? "size: number of series (service + attribute combinations)" : "size: data points received"}</span>} />
        <div className="panel-body">
          <Loads q={size === "cardinality" ? card : list} empty={!tiles.some((t) => t.value > 0)} height={220}>
            {() => <Treemap tiles={tiles} height={230} onTile={(t) => open(t.label)} />}
          </Loads>
        </div>
      </section>
      <Panel title="All metrics" flush right={list.data && <span className="faint">grouped by name · open one to chart it, filter by service and split by attribute</span>}>
        <Loads q={list} empty={!rows.length} height={240}>
          {() => (
            <div className="table-scroll" style={{ maxHeight: 700 }}>
              <table className="dtable">
                <colgroup><col /><col style={{ width: 130 }} /><col style={{ width: 90 }} /><col style={{ width: 120 }} /><col style={{ width: 110 }} /><col style={{ width: 100 }} /></colgroup>
                <thead><tr><th>Name</th><th>Type</th><th>Unit</th><th className="num">Data points</th><th className="num">Cardinality</th><th className="num">Resources</th></tr></thead>
                <tbody>
                  {prefixes.map(([p, ms]) => (
                    <Fragment key={p}>
                      {ms.length > 1 && (
                        <tr className="tree-row" onClick={() => setFolded({ ...folded, [p]: isOpen(p) })}>
                          <td><span className={`caret${isOpen(p) ? " open" : ""}`}>▸</span> <b style={{ fontWeight: 500 }}>{p}</b> <span className="faint">{ms.length} metrics</span></td>
                          <td /><td />
                          <td className="num">{fmtNum(ms.reduce((a, r) => a + Number(r.count), 0))}</td>
                          <td className="num">{cardText(ms.reduce((a, r) => a + (series.get(String(r.metric_name)) ?? 0), 0))}</td>
                          <td />
                        </tr>
                      )}
                      {(ms.length === 1 || isOpen(p)) && ms.map((r) => {
                        const name = String(r.metric_name);
                        return (
                          <tr key={name} className={`tree-row${ms.length > 1 ? " child" : ""}`} onClick={() => open(name)}>
                            <td><span className="link">{name}</span></td>
                            <td className="muted">{typeLabel(info(r))}</td>
                            <td className="muted">{unitLabel(String(r.unit ?? "")) || "—"}</td>
                            <td className="num">{fmtNum(Number(r.count))}</td>
                            <td className="num">{cardText(series.get(name))}</td>
                            <td className="num">{fmtNum(services.get(name) ?? 0)}</td>
                          </tr>
                        );
                      })}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
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

/** Prometheus' histogram_quantile over {upper bound: count}: interpolate inside the bucket holding
 *  the q-th measurement (the first starts at 0; in the +Inf bucket, the highest finite bound). */
export function bucketQuantile(q: number, buckets: Record<string, number>): number | null {
  const bs = Object.entries(buckets).map(([le, c]) => [le === "+Inf" ? Infinity : Number(le), Math.max(0, Number(c) || 0)] as [number, number]).sort((a, b) => a[0] - b[0]);
  const total = bs.reduce((s, [, c]) => s + c, 0);
  if (!bs.length || total <= 0) return null;
  const finite = bs.filter(([le]) => isFinite(le)), top = finite.length ? finite[finite.length - 1][0] : null;
  const rank = q * total;
  let seen = 0;
  for (let i = 0; i < bs.length; i++) {
    const [hi, c] = bs[i];
    if (seen + c >= rank && c > 0) {
      if (!isFinite(hi)) return top;
      const lo = i ? bs[i - 1][0] : hi > 0 ? 0 : hi;
      return lo + ((hi - lo) * (rank - seen)) / c;
    }
    seen += c;
  }
  return top;
}

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
  // Explicit-bucket histograms: the buckets too, for a p95 line (exponential histograms have none)
  const bq: Query = { ...q, aggs: [{ fn: "buckets" }] };
  const withBuckets = p.kind === "histogram" && p.m.type === "histogram";
  const bres = useQuery(withBuckets ? bq : null, JSON.stringify(bq) + p.ctx.tick);
  const unit = unitLabel(p.m.unit);
  const p95 = useMemo(() => {
    const out = new Map<string, Line>();
    for (const row of bres.data?.rows ?? []) {
      const t = Date.parse(String(row[0])), label = p.split ? String(row[1] ?? "(none)") : p.m.name;
      const v = bucketQuantile(0.95, (row[p.split ? 2 : 1] ?? {}) as Record<string, number>);
      if (v == null) continue;
      if (!out.has(label)) out.set(label, { label, points: [] });
      out.get(label)!.points.push([t, v]);
    }
    return [...out.values()];
  }, [bres.data]);   // eslint-disable-line react-hooks/exhaustive-deps

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
          {withBuckets && p95.length > 0 && <Chart title="p95 (from the buckets)" lines={p95} colors={colors} ctx={p.ctx} unit={unit ? ` ${unit}` : ""} />}
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
