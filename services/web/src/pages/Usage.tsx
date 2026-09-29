import { useMemo } from "react";
import { records, Signal } from "../api";
import type { Ctx } from "../App";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { Gauge, Stat } from "../components/Stat";
import { PRICE_LABEL, PRICE_PER_MILLION, usd } from "../pricing";
import { fmtNum } from "../time";
import { Loaded, useQuery } from "../useQuery";

const SIGNALS: Signal[] = ["traces", "logs", "metrics"];
const COLOR: Record<Signal, string> = { traces: "var(--series-3)", logs: "var(--series-2)", metrics: "var(--series-6)" };
const NAME: Record<Signal, string> = { traces: "Spans", logs: "Logs", metrics: "Metrics" };

/** Last 24 hours by service, per signal: the baseline extrapolated to 30 days. */
function useBaseline(ctx: Ctx, signal: Signal): Loaded {
  const w = useMemo(() => {
    const end = Date.now();
    return { start: new Date(end - 86_400_000).toISOString(), end: new Date(end).toISOString() };
  }, [ctx.tick]);
  return useQuery({ signal, ...w, group_by: ["service"], aggs: [{ fn: "count" }], limit: 100 }, signal + "24h" + ctx.tick);
}

export function Usage({ ctx }: { ctx: Ctx }) {
  const q = { traces: useBaseline(ctx, "traces"), logs: useBaseline(ctx, "logs"), metrics: useBaseline(ctx, "metrics") };
  const month = (s: Signal) => (q[s].data ? records(q[s].data!).reduce((a, r) => a + Number(r.count), 0) * 30 : 0);
  const fee = (s: Signal) => (month(s) / 1e6) * PRICE_PER_MILLION[s];
  const ready = SIGNALS.every((s) => q[s].data || q[s].error);
  const total = SIGNALS.reduce((a, s) => a + fee(s), 0);
  const maxMonth = Math.max(1, ...SIGNALS.map(month));

  return (
    <>
      <div className="grid">
        <div className="span-8" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <Panel title="Estimated total, next 30 days">
            {ready ? <Stat value={usd(total)} /> : <div className="skeleton" style={{ height: 84 }} />}
          </Panel>
          <div className="grid">
            {SIGNALS.map((s) => (
              <Panel key={s} title={`${NAME[s]} over 30 days`} span={4}>
                <Loads q={q[s]} height={150}>
                  {() => <Gauge value={month(s)} max={maxMonth} text={fmtNum(month(s))} label={`Total ${PRICE_LABEL[s]}`} color={COLOR[s]} />}
                </Loads>
              </Panel>
            ))}
            {SIGNALS.map((s) => (
              <Panel key={s + "fee"} title={`${NAME[s]} fees`} span={4}>
                <Loads q={q[s]} height={60}>{() => <Stat value={usd(fee(s))} small />}</Loads>
              </Panel>
            ))}
          </div>
        </div>
        <div className="span-4" style={{ padding: "6px 12px" }}>
          <h2 style={{ fontSize: 24, margin: "4px 0 10px", fontWeight: 600 }}>Leasyd cost estimate</h2>
          <h3 style={{ fontSize: 15, margin: "0 0 4px", fontWeight: 600 }}>Extrapolated from the past 24 hours to 30 days</h3>
          <div className="muted" style={{ marginBottom: 18 }}>Always based on the last 24 hours, whatever time range is selected.</div>
          <h3 style={{ fontSize: 15, margin: "0 0 6px", fontWeight: 600 }}>Prices</h3>
          <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 2 }}>
            {SIGNALS.map((s) => <li key={s}><b>${PRICE_PER_MILLION[s].toFixed(2)}</b> <span className="muted">per</span> <b>1M {PRICE_LABEL[s]}</b></li>)}
          </ul>
          <div className="faint" style={{ marginTop: 14, fontSize: 12 }}>Placeholder prices for this estimate; storage and queries are included.</div>
        </div>
      </div>

      {SIGNALS.map((s) => {
        const rows = q[s].data ? records(q[s].data!).map((r) => ({ service: String(r.service ?? "unknown"), n: Number(r.count) * 30 })) : [];
        return (
          <div key={s}>
            <h2 className="section-title">Estimated {NAME[s].toLowerCase()} usage over 30 days</h2>
            <div className="grid">
              <Panel title={`${NAME[s]} by service (top 100)`} span={8} flush>
                <Loads q={q[s]} empty={!rows.length} height={200}>
                  {() => <RankTable head={["service", PRICE_LABEL[s], "fees"]} maxHeight={300} numCols={2}
                                    rows={rows.map((r) => [r.service, fmtNum(r.n), usd((r.n / 1e6) * PRICE_PER_MILLION[s])])} />}
                </Loads>
              </Panel>
              <Panel title={`Total ${PRICE_LABEL[s]}`} span={4}>
                <Loads q={q[s]} height={84}>{() => <Stat value={month(s).toLocaleString("en-US")} small />}</Loads>
              </Panel>
            </div>
          </div>
        );
      })}
    </>
  );
}
