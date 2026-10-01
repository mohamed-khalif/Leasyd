import { FormEvent, useEffect, useMemo, useState } from "react";
import { AlertChannel, AlertRule, alerts, Check, checks, promql as runPromQL, records, RulePreview, Slo, slos } from "../api";
import type { Ctx } from "../App";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { Series, TimeSeries } from "../components/TimeSeries";
import { fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { fmtTs } from "./Logs";

// Alerts: rules (when) and channels (where). Every firing and resolution is also a log of the tenant
// (service "alerts"), shown under History.

export function Alerts({ ctx, path, params }: { ctx: Ctx; path: string; params: URLSearchParams }) {
  const [, , tab, id] = path.split("/");           // /alerts[/rules|/channels|/history][/new|/<id>]
  if (tab === "rules" && id) return <RuleForm ctx={ctx} id={id === "new" ? undefined : id} params={params} />;
  const current = tab === "channels" || tab === "history" ? tab : "rules";
  return (
    <>
      <div className="toolbar">
        <div className="chips">
          {(["rules", "channels", "history"] as const).map((t) => (
            <button key={t} type="button" className={`chip${current === t ? " on" : ""}`} onClick={() => ctx.go(`/alerts/${t}`)}>
              {t === "rules" ? "Rules" : t === "channels" ? "Channels" : "History"}
            </button>
          ))}
        </div>
        <span className="spacer" style={{ flex: 1 }} />
        {current === "rules" && <button className="btn primary" onClick={() => ctx.go("/alerts/rules/new")}>New rule</button>}
      </div>
      {current === "rules" ? <Rules ctx={ctx} /> : current === "channels" ? <Channels ctx={ctx} /> : <History ctx={ctx} />}
    </>
  );
}

function useSettings(tick: number) {
  const [s, setS] = useState<{ rules: AlertRule[]; channels: AlertChannel[]; checks: Check[]; slos: Slo[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    Promise.all([alerts.rules(), alerts.channels(), checks.list(), slos.list()])
      .then(([r, c, k, s]) => setS({ rules: r.items, channels: c.items, checks: k.checks, slos: s.items }), (e: Error) => setError(e.message));
  }, [tick]);
  return { s, error };
}

export function describeRule(r: AlertRule, all: { checks: Check[]; slos: Slo[] }): string {
  if (r.type === "query") {
    const q = (r.promql ?? "").length > 70 ? `${r.promql!.slice(0, 70)}…` : r.promql;
    return `${q} ${r.op} ${r.critical} (critical)${r.degraded != null ? `, ${r.op} ${r.degraded} (degraded)` : ""}${r.for_minutes ? ` for ${r.for_minutes} min` : ""}`;
  }
  if (r.type === "check_failing") {
    const which = r.checks?.[0] === "*" ? "any check" : (r.checks ?? []).map((id) => all.checks.find((c) => c.id === id)?.name ?? "(deleted check)").join(", ");
    return `${which} fails ${r.failures} run${r.failures === 1 ? "" : "s"} in a row`;
  }
  const slo = all.slos.find((s) => s.id === r.slo)?.name ?? "(deleted SLO)";
  const parts = [r.burn_rate != null ? `burns its budget ${r.burn_rate}× too fast (last hour)` : "", r.budget_below != null ? `has less than ${r.budget_below}% of its budget left` : ""];
  return `${slo} ${parts.filter(Boolean).join(" or ")}`;
}

function Rules({ ctx }: { ctx: Ctx }) {
  const { s, error } = useSettings(ctx.tick);
  if (error) return <div className="state error">{error}</div>;
  if (!s) return <div className="skeleton" style={{ height: 160 }} />;
  const names = new Map(s.channels.map((c) => [c.id, c.name]));
  return (
    <Panel title="Alert rules" flush>
      {!s.rules.length ? (
        <div className="state" style={{ minHeight: 180, flexDirection: "column", gap: 10 }}>
          <div>No alert rules yet. A rule sends a message when a check keeps failing, an SLO burns its error budget, or any query (PromQL over logs, spans and metrics) crosses a threshold, and again when it recovers.</div>
          {s.channels.length ? <button className="btn primary" onClick={() => ctx.go("/alerts/rules/new")}>Create a rule</button>
                             : <button className="btn primary" onClick={() => ctx.go("/alerts/channels")}>First, add where alerts go</button>}
        </div>
      ) : (
        <RankTable head={["", "rule", "when", "send to", "now"]} maxHeight={640} onRow={(i) => ctx.go(`/alerts/rules/${s.rules[i].id}`)}
                   rows={s.rules.map((r) => [
                     <span className="dot" style={{ background: !r.enabled ? "var(--text-3)" : r.firing?.length ? "var(--sev-error)" : "var(--ok, #3fb68b)" }} />,
                     <span className="link">{r.name} ›</span>, describeRule(r, s), r.channels.map((c) => names.get(c) ?? "(deleted)").join(", "),
                     !r.enabled ? "off" : r.firing?.length ? <b style={{ color: "var(--sev-error)" }} title={r.firing.join("\n")}>firing ({r.firing.length})</b> : "ok"])} />
      )}
    </Panel>
  );
}

function RuleForm({ ctx, id, params }: { ctx: Ctx; id?: string; params: URLSearchParams }) {
  const { s, error: loadError } = useSettings(0);
  const [r, setR] = useState<Omit<AlertRule, "id">>({
    name: params.get("name") ?? "", type: params.get("promql") ? "query" : params.get("slo") ? "slo_burn" : "check_failing", channels: [], enabled: true,
    checks: params.get("check") ? [params.get("check")!] : ["*"], failures: 2, slo: params.get("slo") ?? undefined, burn_rate: 10, budget_below: 25,
    promql: params.get("promql") ?? "", op: ">", critical: undefined, degraded: null, for_minutes: 2, every_minutes: 1,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (id) alerts.rule(id).then((x) => setR(x), (e: Error) => setError(e.message)); }, [id]);
  useEffect(() => {                                  // a new rule sends to every channel by default
    if (!id && s && !r.channels.length) setR((x) => ({ ...x, channels: s.channels.map((c) => c.id), slo: x.slo ?? s.slos[0]?.id }));
  }, [s]);   // eslint-disable-line react-hooks/exhaustive-deps
  if (loadError) return <div className="state error">{loadError}</div>;
  if (!s) return <div className="skeleton" style={{ height: 240 }} />;
  const set = (p: Partial<AlertRule>) => setR({ ...r, ...p });
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError(null);
    const clean = { slo: undefined, burn_rate: undefined, budget_below: undefined, checks: undefined, failures: undefined,
                    promql: undefined, op: undefined, critical: undefined, degraded: undefined, for_minutes: undefined, every_minutes: undefined };
    const body = r.type === "check_failing" ? { ...r, ...clean, checks: r.checks, failures: r.failures }
      : r.type === "slo_burn" ? { ...r, ...clean, slo: r.slo, burn_rate: r.burn_rate, budget_below: r.budget_below }
      : { ...r, ...clean, promql: r.promql, op: r.op, critical: r.critical, degraded: r.degraded, for_minutes: r.for_minutes, every_minutes: r.every_minutes };
    try {
      if (id) await alerts.updateRule(id, body); else await alerts.addRule(body);
      ctx.go("/alerts/rules");
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!id || !confirm(`Delete the rule “${r.name}”?`)) return;
    try { await alerts.removeRule(id); ctx.go("/alerts/rules"); } catch (err) { setError((err as Error).message); }
  };
  if (!s.channels.length) return (
    <Panel title="New alert rule">
      <div className="state" style={{ minHeight: 140, flexDirection: "column", gap: 10 }}>
        <div>First add where alerts should go: an email address, a Slack channel or a webhook.</div>
        <button className="btn primary" onClick={() => ctx.go("/alerts/channels")}>Add a channel</button>
      </div>
    </Panel>
  );
  return (
    <form onSubmit={submit}>
      <div className="toolbar">
        <a href="#/alerts/rules" className="btn" onClick={(e) => { e.preventDefault(); ctx.go("/alerts/rules"); }}>← Cancel</a>
        <span className="spacer" style={{ flex: 1 }} />
        {id && <button type="button" className="btn" onClick={remove}>Delete</button>}
      </div>
      <Panel title={id ? `Edit “${r.name}”` : "New alert rule"}>
        <section className="form-section">
          <div className="form-grid">
            <label>Name<input className="input" required maxLength={80} value={r.name} onChange={(e) => set({ name: e.target.value })} placeholder="Checkout is down" /></label>
          </div>
          <div className="checks"><label className="check"><input type="checkbox" checked={r.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> On</label></div>
        </section>
        <section className="form-section">
          <h3>When</h3>
          <div className="type-pick" role="radiogroup">
            <button type="button" role="radio" aria-checked={r.type === "check_failing"} className={r.type === "check_failing" ? "on" : ""} onClick={() => set({ type: "check_failing" })}>
              <b>A check keeps failing</b><span>Alert after a number of failed runs in a row; tell you again when it passes.</span>
            </button>
            <button type="button" role="radio" aria-checked={r.type === "slo_burn"} className={r.type === "slo_burn" ? "on" : ""} disabled={!s.slos.length}
                    title={s.slos.length ? "" : "Create an SLO first"} onClick={() => set({ type: "slo_burn" })}>
              <b>An SLO is at risk</b><span>Alert when it uses its error budget too fast, or has little left.</span>
            </button>
            <button type="button" role="radio" aria-checked={r.type === "query"} className={r.type === "query" ? "on" : ""} onClick={() => set({ type: "query" })}>
              <b>A query crosses a threshold</b><span>Any PromQL over logs, spans and metrics: alert on each series that is degraded or critical.</span>
            </button>
          </div>
          {r.type === "query" ? <QueryRuleFields r={r} set={set} ctx={ctx} /> : r.type === "check_failing" ? (<>
            <div className="form-grid" style={{ marginTop: 10 }}>
              <label>Failed runs in a row
                <input className="input" type="number" min={1} max={10} required value={r.failures ?? 2} onChange={(e) => set({ failures: Number(e.target.value) })} />
              </label>
            </div>
            <div className="checks">
              <label className="check"><input type="checkbox" checked={r.checks?.[0] === "*"} onChange={(e) => set({ checks: e.target.checked ? ["*"] : [] })} /> Any check (including ones added later)</label>
            </div>
            {r.checks?.[0] !== "*" && (
              <div className="checks">
                {s.checks.map((c) => <label key={c.id} className="check"><input type="checkbox" checked={r.checks?.includes(c.id) ?? false}
                                                                                 onChange={() => set({ checks: toggle(r.checks ?? [], c.id) })} /> {c.name}</label>)}
              </div>
            )}
            <div className="faint">Runs excluded by hand or during a maintenance window don't count.</div>
          </>) : (
            <div className="form-grid" style={{ marginTop: 10 }}>
              <label>SLO
                <select className="select" value={r.slo ?? ""} onChange={(e) => set({ slo: e.target.value })}>{s.slos.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select>
              </label>
              <label>Burning faster than (× budget rate, last hour; empty: don't check)
                <input className="input" type="number" min={1} max={1000} step={0.1} value={r.burn_rate ?? ""} onChange={(e) => set({ burn_rate: e.target.value === "" ? undefined : Number(e.target.value) })} />
              </label>
              <label>Budget left below (%; empty: don't check)
                <input className="input" type="number" min={0} max={100} value={r.budget_below ?? ""} onChange={(e) => set({ budget_below: e.target.value === "" ? undefined : Number(e.target.value) })} />
              </label>
            </div>
          )}
        </section>
        <section className="form-section">
          <h3>Send to</h3>
          <div className="checks">
            {s.channels.map((c) => <label key={c.id} className="check"><input type="checkbox" checked={r.channels.includes(c.id)} onChange={() => set({ channels: toggle(r.channels, c.id) })} /> {c.name} <span className="faint">({c.type})</span></label>)}
          </div>
        </section>
        {error && <div className="form-error" role="alert" style={{ marginTop: 10 }}>{error}</div>}
        <div className="toolbar" style={{ marginTop: 14 }}><button className="btn primary" disabled={busy}>{busy ? "Saving…" : id ? "Save" : "Create rule"}</button></div>
      </Panel>
    </form>
  );
}

function Channels({ ctx }: { ctx: Ctx }) {
  const [items, setItems] = useState<AlertChannel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [form, setForm] = useState({ type: "email", name: "", email: "", url: "" });
  const [created, setCreated] = useState<AlertChannel | null>(null);
  const [note, setNote] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => { alerts.channels().then((d) => setItems(d.items), (e: Error) => setError(e.message)); }, [ctx.tick, nonce]);
  const add = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError(null);
    try {
      const c = await alerts.addChannel({ type: form.type, name: form.name, ...(form.type === "email" ? { email: form.email } : { url: form.url }) });
      setCreated(c); setForm({ ...form, name: "", email: "", url: "" }); setNonce((n) => n + 1);
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  const test = async (c: AlertChannel) => {
    setNote({ ...note, [c.id]: "sending…" });
    try { const r = await alerts.testChannel(c.id); setNote((n) => ({ ...n, [c.id]: r.sent ? "sent" : `failed: ${r.error}` })); }
    catch (err) { setNote((n) => ({ ...n, [c.id]: (err as Error).message })); }
  };
  const remove = async (c: AlertChannel) => {
    if (!confirm(`Remove “${c.name}”?`)) return;
    try { await alerts.removeChannel(c.id); setNonce((n) => n + 1); } catch (err) { setError((err as Error).message); }
  };
  return (
    <>
      <Panel title="Where alerts go" flush>
        {!items ? <div className="skeleton" style={{ height: 120 }} /> : !items.length ? (
          <div className="state" style={{ minHeight: 100 }}>No channels yet. Add an email address, a Slack channel or a webhook below.</div>
        ) : (
          <RankTable head={["channel", "type", "sends to", "status", ""]} maxHeight={480}
                     rows={items.map((c) => [c.name, c.type, c.email ?? c.url_hint ?? "",
                       c.type === "email" ? (c.status === "confirmed" ? "confirmed" : <span title="We sent a confirmation email; alerts arrive once its link is clicked.">{c.status}</span>) : "ready",
                       <span className="chips" style={{ flexWrap: "nowrap", alignItems: "center" }}>
                         <button type="button" className="btn small" onClick={() => test(c)}>Send a test</button>
                         <button type="button" className="btn small" onClick={() => remove(c)}>Remove</button>
                         {note[c.id] && <span className="faint">{note[c.id]}</span>}
                       </span>])} />
        )}
      </Panel>
      {created?.signing_secret && (
        <div className="result-box pass" role="status">
          <b>Webhook added.</b> Each alert is signed: header <span className="mono">X-Leasyd-Signature: sha256=&lt;HMAC-SHA256 of the body&gt;</span> with this secret.
          It is shown only now:
          <pre className="body-sample">{created.signing_secret}</pre>
        </div>
      )}
      {created?.type === "email" && <div className="result-box pass" role="status"><b>Check {created.email}.</b> AWS sends a confirmation email; alerts arrive after its link is clicked.</div>}
      <Panel title="Add a channel">
        <form onSubmit={add}>
          <div className="form-grid">
            <label>Type
              <select className="select" value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
                <option value="email">Email</option><option value="slack">Slack</option><option value="webhook">Webhook</option>
              </select>
            </label>
            <label>Name<input className="input" required maxLength={80} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                              placeholder={form.type === "email" ? "On-call" : form.type === "slack" ? "#ops-alerts" : "PagerDuty"} /></label>
            {form.type === "email" ? (
              <label>Email address<input className="input" type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></label>
            ) : (
              <label className="wide">{form.type === "slack" ? "Slack incoming-webhook URL" : "Webhook URL (https)"}
                <input className="input mono" required value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })}
                       placeholder={form.type === "slack" ? "https://hooks.slack.com/services/…" : "https://example.com/leasyd-alerts"} />
                <span className="faint">{form.type === "slack" ? "In Slack: Apps → Incoming Webhooks → Add to a channel. Stored encrypted." : "We POST JSON, signed so you can check it came from us. Stored encrypted."}</span>
              </label>
            )}
          </div>
          {error && <div className="form-error" role="alert" style={{ marginTop: 10 }}>{error}</div>}
          <div className="toolbar" style={{ marginTop: 12 }}><button className="btn primary" disabled={busy}>{busy ? "Adding…" : "Add channel"}</button></div>
        </form>
      </Panel>
    </>
  );
}

function History({ ctx }: { ctx: Ctx }) {
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const q = useQuery({ signal: "logs", ...w, services: ["alerts"], search: { limit: 500 } }, "h" + ctx.range.key + ctx.tick);
  const rows = q.data ? records(q.data).sort((a, b) => String(b.ts).localeCompare(String(a.ts))) : [];
  return (
    <Panel title="Alert history" flush right={<span className="faint">in the time range above</span>}>
      <Loads q={q} empty={!rows.length} height={160}>
        {() => <RankTable head={["time", "", "rule", "what", "detail"]} maxHeight={640} numCols={0}
                          onRow={(i) => { const u = String((rows[i].attributes as Record<string, unknown>)["alert.url"] ?? ""); const h = u.split("#")[1]; if (h) ctx.go(h); }}
                          rows={rows.map((r) => {
                            const a = r.attributes as Record<string, unknown>, firing = a["alert.state"] === "firing";
                            return [fmtTs(String(r.ts)), <b style={{ color: firing ? "var(--sev-error)" : "var(--ok, #3fb68b)" }}>{firing ? "firing" : "resolved"}</b>,
                                    String(a["alert.rule"] ?? ""), String(a["alert.subject"] ?? ""),
                                    String(r.body ?? "").replace(/^[A-Z]+: [^.]*\. /, "") + (a["alert.failed_channels"] ? ` (not delivered to ${a["alert.failed_channels"]})` : "")];
                          })} />}
      </Loads>
    </Panel>
  );
}

const OPS: [string, string][] = [[">", "above"], [">=", "at or above"], ["<", "below"], ["<=", "at or below"], ["==", "equal to"], ["!=", "not equal to"]];

/** A check rule: the query, its thresholds, and what it gives now (chart of the last 3 hours, each series' level). */
function QueryRuleFields({ r, set, ctx }: { r: Omit<AlertRule, "id">; set: (p: Partial<AlertRule>) => void; ctx: Ctx }) {
  const [shown, setShown] = useState(r.promql ?? "");
  const [chart, setChart] = useState<{ series: Series[]; error?: string } | null>(null);
  const [now, setNow] = useState<RulePreview | { error: string } | null>(null);
  const run = (text: string) => {
    setShown(text); setChart(null); setNow(null);
    if (!text.trim()) return;
    const end = Date.now(), start = end - 3 * 3600_000;
    runPromQL({ promql: text, start: new Date(start).toISOString(), end: new Date(end).toISOString(), step: 60 }).then(
      (res) => setChart({ series: res.data.result.slice(0, 10).map((s, i) => ({
        label: Object.entries(s.metric).filter(([k]) => k !== "__name__").map(([k, v]) => `${k}=${v}`).join(", ") || "value",
        color: `var(--series-${(i % 6) + 1})`, points: s.values.map(([t, v]) => [t * 1000, Number(v)] as [number, number]).filter((p) => isFinite(p[1])) })) }),
      (e: Error) => setChart({ series: [], error: e.message }));
  };
  useEffect(() => { if (r.promql) run(r.promql); }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!shown.trim() || r.critical == null || Number.isNaN(r.critical)) return;
    let live = true;
    alerts.preview({ promql: shown, op: r.op ?? ">", critical: r.critical, degraded: r.degraded ?? null })
      .then((p) => live && setNow(p), (e: Error) => live && setNow({ error: e.message }));
    return () => { live = false; };
  }, [shown, r.op, r.critical, r.degraded]);
  const num = (v: string) => (v === "" ? undefined : Number(v));
  const range = { key: "3h", label: "Last 3 hours", minutes: 180 };
  return (
    <div style={{ marginTop: 10 }}>
      <label className="step-body block" style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 12.5, color: "var(--text-2)" }}>PromQL
        <textarea className="input mono qb-editor" rows={3} required spellCheck={false} value={r.promql ?? ""} onChange={(e) => set({ promql: e.target.value })}
                  placeholder='100 * sum by (service_name) (rate(leasyd.spans{status_code="ERROR"}[5m])) / sum by (service_name) (rate(leasyd.spans[5m]))' />
      </label>
      <div className="qb-editor-bar">
        <button type="button" className="btn" onClick={() => run(r.promql ?? "")}>Preview</button>
        <a href={`#/query?promql=${encodeURIComponent(r.promql ?? "")}`} onClick={(e) => { e.preventDefault(); ctx.go(`/query?promql=${encodeURIComponent(r.promql ?? "")}`); }}>Open in Query Builder</a>
        <span className="faint">Checked every {r.every_minutes ?? 1} min on the data as of a minute before. Use a range like [5m] for rates.</span>
      </div>
      <div className="form-grid" style={{ marginTop: 10, gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
        <label>Alert when the value is
          <select className="select" value={r.op ?? ">"} onChange={(e) => set({ op: e.target.value })}>{OPS.map(([o, l]) => <option key={o} value={o}>{l} ({o})</option>)}</select>
        </label>
        <label>Critical at
          <input className="input" type="number" step="any" required value={r.critical ?? ""} onChange={(e) => set({ critical: num(e.target.value) })} placeholder="5" />
        </label>
        <label>Degraded at (optional)
          <input className="input" type="number" step="any" value={r.degraded ?? ""} onChange={(e) => set({ degraded: e.target.value === "" ? null : Number(e.target.value) })} placeholder="2" />
        </label>
        <label>For at least
          <select className="select" value={r.for_minutes ?? 0} onChange={(e) => set({ for_minutes: Number(e.target.value) })}>
            {[0, 1, 2, 5, 10, 15, 30, 60].map((m) => <option key={m} value={m}>{m ? `${m} min` : "at once"}</option>)}
          </select>
        </label>
        <label>Check every
          <select className="select" value={r.every_minutes ?? 1} onChange={(e) => set({ every_minutes: Number(e.target.value) })}>
            {[1, 5, 15].map((m) => <option key={m} value={m}>{m} min</option>)}
          </select>
        </label>
      </div>
      <div className="panel" style={{ marginTop: 12 }}>
        <header className="panel-head"><span>Last 3 hours</span><span className="spacer" />
          <span className="faint"><i className="swatch" style={{ background: "var(--sev-error)" }} />critical{r.degraded != null ? <> <i className="swatch" style={{ background: "var(--sev-warn)", marginLeft: 8 }} />degraded</> : null}</span></header>
        <div className="panel-body">
          {!shown.trim() ? <div className="state" style={{ minHeight: 120 }}>Write a query and press Preview</div>
            : !chart ? <div className="skeleton" style={{ height: 180 }} />
            : chart.error ? <div className="state error" style={{ minHeight: 120 }}>{chart.error}</div>
            : !chart.series.length ? <div className="state" style={{ minHeight: 120 }}>The query returns nothing in the last 3 hours</div>
            : <TimeSeries series={chart.series} range={range} height={180}
                          thresholds={[...(r.critical != null && !Number.isNaN(r.critical) ? [{ value: r.critical, color: "var(--sev-error)" }] : []),
                                       ...(r.degraded != null ? [{ value: r.degraded, color: "var(--sev-warn)" }] : [])]} />}
        </div>
      </div>
      {now && (
        <div className="panel" style={{ marginTop: 12 }}>
          <header className="panel-head"><span>Right now</span></header>
          {"error" in now ? <div className="state error" style={{ minHeight: 60 }}>{now.error}</div> : !now.series.length
            ? <div className="state" style={{ minHeight: 60 }}>No series: nothing would fire</div> : (
            <div className="table-scroll" style={{ maxHeight: 220 }}>
              <table className="dtable"><colgroup><col /><col style={{ width: 120 }} /><col style={{ width: 110 }} /></colgroup>
                <thead><tr><th>Series</th><th className="num">Value</th><th>Would be</th></tr></thead>
                <tbody>{now.series.map((x) => (
                  <tr key={x.name} style={{ cursor: "default" }}><td>{x.name}</td><td className="num">{fmtNum(x.value)}</td>
                    <td><span className={`pill ${x.level === "critical" ? "bad" : x.level === "ok" ? "ok" : ""}`}
                              style={x.level === "degraded" ? { color: "var(--sev-warn)", borderColor: "var(--sev-warn)" } : undefined}>{x.level}</span></td></tr>))}</tbody>
              </table>
            </div>)}
        </div>
      )}
    </div>
  );
}
