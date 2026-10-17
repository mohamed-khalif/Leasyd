// Pieces of the span view shared by Traces and Lambda: the span type, a key/value list, and a
// span's duration against its operation's other spans.
import { records } from "../api";
import { fmtMs, fmtNum } from "../lib/format";
import { bucketLow, LOG_STEPS } from "./Charts";
import { Loads } from "./Panel";
import { useQuery } from "../useQuery";

export type SpanEvent = { at: number; name: string; attributes: Record<string, string> };   // at: ns since the epoch
export type Span = { span_id: string; parent_span_id?: string; name: string; service: string; start: number; dur: number; status?: string; depth: number;
              events: SpanEvent[]; raw: Record<string, unknown> };

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
