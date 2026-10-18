// Settings › Billing: the plan (trial, pay as you go, ended), prices, an estimate of this month so
// far, and Stripe for payment details, card and invoices.
import { useEffect, useState } from "react";
import { fmtDay, fmtUnitPrice, fmtUsd } from "../../lib/format";
import { account, Account, Meter, Signal } from "../../api";
import { PRICE, PRICE_LABEL } from "../../pricing";
import { ownersOnly, Section, SettingsPage, SettingsProps } from "./parts";

const SIGNALS: Signal[] = ["logs", "traces", "metrics"];
const NAME: Record<Signal, string> = { logs: "Logs", traces: "Traces", metrics: "Metrics" };

const BILLING_STATUS: Record<string, string> = {
  past_due: "Payment overdue: please update your card.",
  unpaid: "Unpaid: data is no longer accepted.", canceled: "Cancelled: data is no longer accepted.",
};

/** The plan, in one line, and how it reads (ok, warn, error). */
function planNotice(acc: Account, now = Date.now()): { kind: "ok" | "warn" | "error"; text: string } {
  const b = acc.billing, subscribed = b?.status === "active" || b?.status === "trialing" || b?.status === "past_due";
  if (b?.status && BILLING_STATUS[b.status]) return { kind: "error", text: BILLING_STATUS[b.status] };
  if (subscribed) return { kind: "ok", text: "Pay as you go: billed monthly for what you use." };
  if (acc.trial_ends_at) {
    const ends = Date.parse(acc.trial_ends_at), days = Math.ceil((ends - now) / 86_400_000);
    if (days > 0) return { kind: days <= 3 ? "warn" : "ok", text: `Free trial: ${days} ${days === 1 ? "day" : "days"} left (ends ${fmtDay(ends)}). Add payment details to keep your data flowing after it.` };
    return { kind: "error", text: `Your free trial ended on ${fmtDay(ends)}. Add payment details to start sending data again.` };
  }
  return { kind: "ok", text: acc.plan === "free" ? "Free plan." : `Plan: ${acc.plan}.` };
}

/** This month so far (UTC), from the account's meter: ingest on everything received, storage on what was kept. */
function useMonthSoFar(tick: number): { cost: number; days: number } | null | "error" {
  const [days, setDays] = useState<Meter[] | null | "error">(null);
  useEffect(() => {
    let live = true;
    account.meters(new Date().getUTCDate()).then((r) => live && setDays(r.days), () => live && setDays("error"));
    return () => { live = false; };
  }, [tick]);
  if (days === null || days === "error") return days;
  const month = new Date().toISOString().slice(0, 7), mine = days.filter((d) => d.day.startsWith(month));
  const cost = mine.reduce((sum, d) => sum + SIGNALS.reduce((a, s) => {
    const inn = d[`in_${s}`] ?? 0, kept = inn - (d[`dropped_${s}`] ?? 0);
    return a + (inn / 1e6) * PRICE[s].ingest + (kept / 1e6) * PRICE[s].storage;
  }, 0), 0);
  return { cost, days: mine.length };
}

export function Billing({ ctx, acc, owner, busy, act, setNote, reload }: SettingsProps) {
  const b = acc.billing;
  const subscribed = b?.status === "active" || b?.status === "trialing" || b?.status === "past_due";
  const notice = planNotice(acc);
  const soFar = useMonthSoFar(ctx.tick);
  const toStripe = (f: () => Promise<{ url: string }>) => act(async () => { window.location.assign((await f()).url); });

  // Back from Stripe Checkout: the subscription starts when Stripe tells us (seconds); look again shortly.
  const back = /[?&]billing=(done|cancelled)/.exec(window.location.hash)?.[1];
  useEffect(() => {
    if (back !== "done") return undefined;
    setNote("Thanks: your payment details are saved. Your subscription starts in a few seconds.");
    const t = [3000, 8000].map((ms) => window.setTimeout(reload, ms));
    return () => t.forEach(window.clearTimeout);
  }, [back, reload, setNote]);   // both stable

  return (
    <SettingsPage title="Billing">
      <div className={`set-notice ${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}>{notice.text}</div>

      <Section title="Payment" about={subscribed
        ? "Your card, invoices and receipts are managed by Stripe, our payment provider."
        : "No upfront charge: you're billed monthly for what you use. Stripe, our payment provider, keeps your card details."}>
        {!b?.enabled ? <p className="faint">Online billing isn't turned on for this account. Email support@leasyd.com to set up a plan.</p>
          : owner ? (
            <div className="billing-actions">
              {!subscribed && <button className="btn primary" disabled={busy} onClick={() => toStripe(account.checkout)}>Add payment details</button>}
              {b.has_customer && <button className="btn" disabled={busy} onClick={() => toStripe(account.portal)}>Manage card and invoices</button>}
            </div>
          ) : ownersOnly("change billing")}
      </Section>

      <Section title="This month so far" about={<>An estimate from the data received since the 1st (UTC). Check runs are not included;
        your invoice is final. <a href="#/settings/usage">See the 30-day estimate in Usage &amp; cost</a>.</>}>
        {soFar === "error" ? <p className="form-error">Couldn't read this month's usage. Try Refresh.</p>
          : soFar ? <><div className="plan-num">{fmtUsd(soFar.cost)}</div><div className="faint">over {soFar.days} {soFar.days === 1 ? "day" : "days"}</div></>
          : <div className="skeleton" style={{ height: 40, maxWidth: 200 }} />}
      </Section>

      <Section title="Prices" about={<>Two parts: ingest on every record received, dropped ones too, and storage on what is kept for 30 days.
        <> </><a href="https://www.leasyd.com/pricing.html" target="_blank" rel="noreferrer">Full pricing</a></>}>
        <table className="dtable">
          <thead><tr><th>Per million</th><th className="num">Ingest</th><th className="num">Storage</th></tr></thead>
          <tbody>
            {SIGNALS.map((s) => (
              <tr key={s}><td>{NAME[s]} <span className="faint">({PRICE_LABEL[s]})</span></td>
                <td className="num">{fmtUnitPrice(PRICE[s].ingest)}</td><td className="num">{fmtUnitPrice(PRICE[s].storage)}</td></tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Enterprise" about="Large volumes, a contract or invoice terms? Talk to us.">
        <a className="btn" href="mailto:support@leasyd.com?subject=Leasyd%20Enterprise&body=Hi%2C%20I%27d%20like%20a%20demo%20of%20Leasyd%20Enterprise.%20Good%20times%20for%20me%3A%20">Book a demo</a>
      </Section>
    </SettingsPage>
  );
}
