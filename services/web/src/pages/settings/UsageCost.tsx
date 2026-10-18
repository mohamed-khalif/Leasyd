// Settings › Usage & cost: today against the plan's daily limits, then the cost of the next 30 days
// at today's pace.
import { useEffect, useMemo, useState } from "react";
import { fmtInt, fmtNum, fmtUnit, fmtUnitPrice, NONE } from "../../lib/format";
import { account, Meter, records, Signal } from "../../api";
import type { Ctx } from "../../App";
import { Loads, Panel } from "../../components/Panel";
import { RankTable } from "../../components/RankTable";
import { Gauge, Stat } from "../../components/Stat";
import { PRICE, PRICE_LABEL, usd } from "../../pricing";
import { Loaded, useQuery } from "../../useQuery";
import { useNow } from "../../useNow";
import { Section, SettingsPage, SettingsProps } from "./parts";

const SIGNALS: Signal[] = ["traces", "logs", "metrics"];
const COLOR: Record<Signal, string> = { traces: "var(--series-3)", logs: "var(--series-2)", metrics: "var(--series-6)" };
const NAME: Record<Signal, string> = { traces: "Spans", logs: "Logs", metrics: "Metrics" };

/** Last 24 hours by service, per signal: the baseline extrapolated to 30 days. */
function useBaseline(ctx: Ctx, signal: Signal): Loaded {
  const end = useNow(ctx.tick);
  const w = useMemo(() => ({ start: new Date(end - 86_400_000).toISOString(), end: new Date(end).toISOString() }), [end]);
  return useQuery({ signal, ...w, group_by: ["service"], aggs: [{ fn: "count" }], limit: 100 }, signal + "24h" + ctx.tick);
}

/** The share of each signal's records that drop rules discard, from the account's meter: yesterday's
 * (a whole day) if metered, else today's so far. Applied to what was stored, as dropped / kept. */
function useDropShare(ctx: Ctx): Record<Signal, number> | null | "error" {
  const [days, setDays] = useState<Meter[] | null | "error">(null);
  useEffect(() => { account.meters(2).then((r) => setDays(r.days), () => setDays("error")); }, [ctx.tick]);
  if (!days || days === "error") return days;
  const [today, yesterday] = days;
  const ratio = (s: Signal) => {
    const m = yesterday && (yesterday[`in_${s}`] ?? 0) > 0 ? yesterday : today;
    const inn = m?.[`in_${s}`] ?? 0, out = m?.[`dropped_${s}`] ?? 0;
    return inn > out ? out / (inn - out) : 0;   // dropped per record kept
  };
  return { traces: ratio("traces"), logs: ratio("logs"), metrics: ratio("metrics") };
}

/** Fees for a month: ingest on everything received (stored + dropped), storage on what is kept. */
const fees = (s: Signal, stored: number, dropped = 0) =>
  ((stored + dropped) / 1e6) * PRICE[s].ingest + (stored / 1e6) * PRICE[s].storage;

/** The cost estimate: the last 24 hours by service, extrapolated to 30 days, at list prices. Settings
 *  shows it under the Usage page without waiting for the account (it doesn't need it). */
export function UsageCost({ ctx }: { ctx: Ctx }) {
  const q = { traces: useBaseline(ctx, "traces"), logs: useBaseline(ctx, "logs"), metrics: useBaseline(ctx, "metrics") };
  const drops = useDropShare(ctx), dropShare = drops === "error" ? null : drops;
  const month = (s: Signal) => (q[s].data ? records(q[s].data!).reduce((a, r) => a + Number(r.count), 0) * 30 : 0);
  const droppedMonth = (s: Signal) => (dropShare ? month(s) * dropShare[s] : 0);
  const fee = (s: Signal) => fees(s, month(s), droppedMonth(s));
  const saved = SIGNALS.reduce((a, s) => a + (droppedMonth(s) / 1e6) * PRICE[s].storage, 0);
  const ready = SIGNALS.every((s) => q[s].data || q[s].error);
  const failed = SIGNALS.filter((s) => q[s].error);    // a signal that can't be read would count as $0: no total then
  const total = SIGNALS.reduce((a, s) => a + fee(s), 0);
  const maxMonth = Math.max(1, ...SIGNALS.map(month));

  return (
    <>
      <div className="grid">
        <div className="span-8" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <Panel title="Estimated total, next 30 days">
            {!ready ? <div className="skeleton" style={{ height: 84 }} />
              : failed.length ? <Stat value={NONE} sub={`couldn't load ${failed.map((s) => NAME[s].toLowerCase()).join(", ")}: no total without them`} />
              : <Stat value={usd(total)} sub={drops === "error" ? "couldn't load what drop rules discard: this leaves out their ingest fee"
                                                : saved >= 0.01 ? `${usd(saved)} saved by drop rules` : undefined} />}
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
          <h3 style={{ fontSize: 15, margin: "0 0 6px", fontWeight: 600 }}>Prices per million</h3>
          <table className="price-mini">
            <thead><tr><th /><th>Ingest</th><th>Storage (30 days)</th></tr></thead>
            <tbody>{SIGNALS.map((s) => (
              <tr key={s}><td>{PRICE_LABEL[s]}</td><td>{fmtUnitPrice(PRICE[s].ingest)}</td><td>{fmtUnitPrice(PRICE[s].storage)}</td></tr>
            ))}</tbody>
          </table>
          <div className="faint" style={{ marginTop: 14, fontSize: 12 }}>
            Ingest is charged on everything received; storage only on what you keep. Data dropped by{" "}
            <a href="#/drop-rules" onClick={(e) => { e.preventDefault(); ctx.go("/drop-rules"); }}>drop rules</a> costs ingest only.
            {SIGNALS.some((s) => droppedMonth(s)) ? ` Dropping about ${fmtNum(SIGNALS.reduce((a, s) => a + droppedMonth(s), 0) / 30)} records a day.` : ""}
          </div>
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
                                    rows={rows.map((r) => [r.service, fmtNum(r.n), usd(fees(s, r.n))])} />}
                </Loads>
              </Panel>
              <Panel title={`Total ${PRICE_LABEL[s]}`} span={4}>
                <Loads q={q[s]} height={84}>{() => <Stat value={fmtInt(month(s))} small />}</Loads>
              </Panel>
            </div>
          </div>
        );
      })}
    </>
  );
}

const meterColor = (f: number) => (f >= 1 ? "var(--sev-error)" : f > 0.8 ? "var(--sev-warn)" : "var(--accent)");

export function Usage({ acc }: SettingsProps) {
  const cap = acc.daily_cap_bytes ?? null, used = acc.today?.bytes ?? 0;
  const share = cap ? Math.min(1, used / cap) : 0;
  const allowance = acc.searches?.units_per_day ?? null, searched = acc.searches?.units_today ?? 0;
  const searchShare = allowance ? Math.min(1, searched / allowance) : 0;
  return (
    <SettingsPage title="Usage & cost" lead={<>Today against your plan's daily limits, then what the next 30 days would cost at today's pace.</>}>
      <Section title="Data received today" about={cap ? `Your plan takes up to ${fmtUnit(cap, "bytes")} of logs, traces and metrics a day (UTC). Over that, new data is refused until 00:00 UTC.` : "No daily limit."}>
        <div className="plan-usage">
          <div className="plan-num">{fmtUnit(used, "bytes")}{cap ? <span className="faint"> of {fmtUnit(cap, "bytes")}</span> : null}</div>
          {cap ? <div className="meter"><i style={{ width: `${share * 100}%`, background: meterColor(share) }} /></div> : null}
          <div className="faint">{fmtNum(acc.today?.records ?? 0)} records{acc.today?.refused_bytes ? ` · ${fmtUnit(acc.today.refused_bytes, "bytes")} refused over the limit` : ""}</div>
        </div>
        {cap && share >= 1 && <div className="form-error">Today's limit is reached: new data is refused until 00:00 UTC.</div>}
      </Section>
      {allowance ? (
        <Section title="Searches today" about="Each search uses units; longer time ranges use more. The allowance resets at 00:00 UTC.">
          <div className="plan-usage">
            <div className="plan-num">{Math.round(searchShare * 100)}%<span className="faint"> of the daily allowance</span></div>
            <div className="meter"><i style={{ width: `${searchShare * 100}%`, background: meterColor(searchShare) }} /></div>
            <div className="faint">{fmtNum(searched)} of {fmtNum(allowance)} search units</div>
          </div>
          {searchShare >= 1 && <div className="form-error">Today's search allowance is used up: searches resume at 00:00 UTC.</div>}
        </Section>
      ) : null}
    </SettingsPage>
  );
}
