// Drop rules: data discarded as it arrives (debug logs, health-check spans, noisy metrics), billed for
// ingest only, never stored. Rules apply in order: the first enabled rule a record matches keeps
// keep_percent of such records (0 drops them all; sampling keeps whole traces). Owners change them;
// everyone sees them. A rule's preview counts what it would have matched in the last 24 hours of
// stored data (a query with the same conditions), and what that would save a month.
import { useEffect, useMemo, useState } from "react";
import { account, DropCondition, DropRule, Meter, query, records, Signal } from "../api";
import type { Ctx } from "../App";
import { Panel } from "../components/Panel";
import { PRICE, PRICE_LABEL, usd } from "../pricing";
import { fmtNum } from "../time";

const SIGNALS: Signal[] = ["logs", "traces", "metrics"];
const SIGNAL_NAME: Record<Signal, string> = { logs: "Logs", traces: "Spans", metrics: "Metrics" };
const FIELDS: Record<Signal, [string, string][]> = {
  logs: [["service", "Service"], ["severity_number", "Severity"], ["severity_text", "Severity text"], ["body", "Message"]],
  traces: [["service", "Service"], ["name", "Span name"], ["kind", "Span kind"], ["status_code", "Status"], ["duration_ns", "Duration (ms)"]],
  metrics: [["service", "Service"], ["metric_name", "Metric name"]],
};
const OPS: [DropCondition["op"], string][] = [["=", "is"], ["!=", "is not"], ["contains", "contains"], ["in", "is one of"],
  ["<", "<"], ["<=", "≤"], [">", ">"], [">=", "≥"]];
const NUMERIC = new Set(["severity_number", "kind", "status_code", "duration_ns"]);
const SEVERITIES: [number, string][] = [[5, "DEBUG"], [9, "INFO"], [13, "WARN"], [17, "ERROR"], [21, "FATAL"]];
const KINDS: [number, string][] = [[1, "INTERNAL"], [2, "SERVER"], [3, "CLIENT"], [4, "PRODUCER"], [5, "CONSUMER"]];
const STATUSES: [number, string][] = [[0, "UNSET"], [1, "OK"], [2, "ERROR"]];

const TEMPLATES: { label: string; rule: DropRule }[] = [
  { label: "Debug and trace logs", rule: { name: "Debug and trace logs", signal: "logs", enabled: true, keep_percent: 0,
    conditions: [{ field: "severity_number", op: "<", value: 9 }] } },
  { label: "Health-check spans", rule: { name: "Health-check spans", signal: "traces", enabled: true, keep_percent: 0,
    conditions: [{ field: "attributes.http.route", op: "in", value: ["/health", "/healthz", "/ready", "/readyz", "/livez", "/ping"] }] } },
  { label: "Keep 10% of fast successful spans", rule: { name: "Sample fast successful spans", signal: "traces", enabled: true, keep_percent: 10,
    conditions: [{ field: "status_code", op: "!=", value: 2 }, { field: "duration_ns", op: "<", value: 10_000_000 }] } },
  { label: "A noisy metric", rule: { name: "Noisy metric", signal: "metrics", enabled: true, keep_percent: 0,
    conditions: [{ field: "metric_name", op: "=", value: "" }] } },
  { label: "Custom rule", rule: { name: "", signal: "logs", enabled: true, keep_percent: 0,
    conditions: [{ field: "service", op: "=", value: "" }] } },
];

type Preview = { matched: number; total: number } | "error" | "loading";

export function DropRules({ ctx }: { ctx: Ctx }) {
  const [rules, setRules] = useState<DropRule[] | null>(null);
  const [saved, setSaved] = useState<string>("[]");
  const [owner, setOwner] = useState(false);
  const [limits, setLimits] = useState({ rules: 50, conditions: 5 });
  const [today, setToday] = useState<Meter | null>(null);
  const [editing, setEditing] = useState<{ index: number; rule: DropRule } | null>(null);
  const [previews, setPreviews] = useState<Record<string, Preview>>({});
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    account.dropRules().then((r) => { setRules(r.rules); setSaved(JSON.stringify(r.rules)); setLimits(r.limits); },
      (e: Error) => setError(e.message));
    account.get().then((a) => setOwner(a.you.role === "owner"), () => undefined);
    account.meters(1).then((r) => setToday(r.days[0] ?? null), () => undefined);
  }, [ctx.tick]);

  const preview = (r: DropRule) => {
    const rule = normalized(r);
    const key = JSON.stringify([rule.signal, rule.conditions]);
    if (previews[key] && previews[key] !== "error") return;
    setPreviews((p) => ({ ...p, [key]: "loading" }));
    const end = new Date(), start = new Date(end.getTime() - 86_400_000);
    const w = { signal: rule.signal, start: start.toISOString(), end: end.toISOString(), aggs: [{ fn: "count" }] };
    Promise.all([query({ ...w, where: rule.conditions.map(toWhere) }), query(w)]).then(([m, t]) => {
      const n = (r: typeof m) => Number(records(r)[0]?.count ?? 0);
      setPreviews((p) => ({ ...p, [key]: { matched: n(m), total: n(t) } }));
    }, () => setPreviews((p) => ({ ...p, [key]: "error" })));
  };
  const previewOf = (r: DropRule) => { const rule = normalized(r); return previews[JSON.stringify([rule.signal, rule.conditions])]; };
  useEffect(() => { rules?.forEach(preview); }, [rules]);   // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = rules != null && JSON.stringify(rules) !== saved;
  const save = async (next: DropRule[]) => {
    setBusy(true); setError(null); setNote(null);
    try {
      const r = await account.saveDropRules(next);
      setRules(r.rules); setSaved(JSON.stringify(r.rules)); setEditing(null);
      setNote("Saved. Ingest applies the rules within about a minute.");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const update = (next: DropRule[]) => (owner ? save(next) : undefined);

  if (!rules) return error ? <div className="state error">{error}</div> : <div className="skeleton" style={{ height: 300 }} />;

  return (
    <div className="drop">
      <div className="drop-head">
        <h2>Drop rules</h2>
        <p className="muted">
          Discard data you don't need as it arrives: debug logs, health checks, noisy metrics. Dropped data is charged
          for ingest only (from ${PRICE.metrics.ingest.toFixed(3)} per million) and never stored, so it's never billed for storage.
          Rules apply in order: the first one a record matches decides.
        </p>
      </div>
      {error && <div className="form-error" role="alert">{error}</div>}
      {note && <div className="form-note" role="status">{note}</div>}

      <div className="drop-cards">
        {SIGNALS.map((s) => {
          const inn = today?.[`in_${s}`] ?? 0, out = today?.[`dropped_${s}`] ?? 0;
          return (
            <div key={s} className="drop-card">
              <span className="faint">{SIGNAL_NAME[s]} dropped today (UTC)</span>
              <b>{fmtNum(out)}</b>
              <span className="faint">{inn ? `of ${fmtNum(inn)} received (${((out / inn) * 100).toFixed(1)}%) · saves ${usd((out / 1e6) * PRICE[s].storage)} today` : "nothing received yet today"}</span>
            </div>
          );
        })}
      </div>

      <Panel title={`Rules (${rules.length} of ${limits.rules})`} flush
             right={owner && dirty ? <button className="btn primary small" disabled={busy} onClick={() => save(rules)}>Save order</button> : undefined}>
        {rules.length === 0 ? (
          <div className="state" style={{ minHeight: 90 }}>No drop rules yet: everything you send is kept.</div>
        ) : (
          <table className="dtable" style={{ fontFamily: "inherit" }}>
            <thead><tr><th style={{ width: 54 }}>On</th><th style={{ width: "22%" }}>Rule</th><th>Drops</th><th style={{ width: 90 }}>Keeps</th>
              <th style={{ width: "21%" }}>Last 24 hours</th>{owner && <th style={{ width: 150 }} />}</tr></thead>
            <tbody>{rules.map((r, i) => {
              const p = previewOf(r);
              return (
                <tr key={r.id ?? i} style={{ cursor: "default" }}>
                  <td><button className={`switch${r.enabled ? " on" : ""}`} disabled={!owner || busy} aria-label={r.enabled ? "Turn off" : "Turn on"}
                              onClick={() => update(rules.map((x, j) => (j === i ? { ...x, enabled: !x.enabled } : x)))} /></td>
                  <td title={r.name}><b>{r.name}</b><div className="faint">{SIGNAL_NAME[r.signal]}</div></td>
                  <td><div className="drop-conds">{r.conditions.map((c, j) => <span key={j} className="drop-cond">{describe(c)}</span>)}</div></td>
                  <td>{r.keep_percent ? `${r.keep_percent}%` : "none"}</td>
                  <td><PreviewText p={p} rule={r} /></td>
                  {owner && (
                    <td className="num">
                      <button className="linkbtn" disabled={i === 0 || busy} onClick={() => setRules(move(rules, i, -1))} title="Earlier">▲</button>{" "}
                      <button className="linkbtn" disabled={i === rules.length - 1 || busy} onClick={() => setRules(move(rules, i, 1))} title="Later">▼</button>{" "}
                      <button className="linkbtn" onClick={() => setEditing({ index: i, rule: structuredClone(r) })}>Edit</button>{" "}
                      <button className="linkbtn danger" disabled={busy} onClick={() => update(rules.filter((_, j) => j !== i))}>Delete</button>
                    </td>
                  )}
                </tr>
              );
            })}</tbody>
          </table>
        )}
      </Panel>

      {owner && !editing && rules.length < limits.rules && (
        <Panel title="Add a rule">
          <div className="drop-templates">
            {TEMPLATES.map((t) => (
              <button key={t.label} className="btn" onClick={() => setEditing({ index: -1, rule: structuredClone(t.rule) })}>{t.label}</button>
            ))}
          </div>
        </Panel>
      )}
      {!owner && <p className="faint">Only the account's owners can change drop rules.</p>}

      {editing && (
        <Editor rule={editing.rule} maxConditions={limits.conditions} busy={busy} preview={previewOf(editing.rule)}
                onPreview={() => preview(editing.rule)} onChange={(rule) => setEditing({ ...editing, rule })}
                onCancel={() => setEditing(null)}
                onSave={() => { const r = normalized(editing.rule); save(editing.index < 0 ? [...rules, r] : rules.map((x, j) => (j === editing.index ? r : x))); }} />
      )}
    </div>
  );
}

function move<T>(list: T[], i: number, by: number): T[] {
  const out = [...list];
  [out[i], out[i + by]] = [out[i + by], out[i]];
  return out;
}

/** A condition as a query filter (the engine's fields and ops; duration is entered in ms). */
function toWhere(c: DropCondition) {
  return { field: c.field, op: c.op, value: c.value };
}

/** "in" values are typed as comma-separated text in the editor; a list when saved or counted. */
function normalized(rule: DropRule): DropRule {
  return { ...rule, conditions: rule.conditions.map((c) => (c.op === "in" && !Array.isArray(c.value)
    ? { ...c, value: String(c.value).split(",").map((x) => x.trim()).filter(Boolean) } : c)) };
}

function label(field: string) {
  const known = Object.values(FIELDS).flat().find(([f]) => f === field);
  if (known) return known[1].toLowerCase();
  if (field.startsWith("attributes.")) return field.slice("attributes.".length);
  if (field.startsWith("resource.")) return `resource ${field.slice("resource.".length)}`;
  return field;
}

function valueText(field: string, v: DropCondition["value"]) {
  const named = (list: [number, string][]) => list.find(([n]) => n === Number(v))?.[1] ?? String(v);
  if (Array.isArray(v)) return v.join(", ");
  if (field === "severity_number") return named(SEVERITIES);
  if (field === "kind") return named(KINDS);
  if (field === "status_code") return named(STATUSES);
  if (field === "duration_ns") return `${Number(v) / 1e6} ms`;
  return `"${v}"`;
}

function describe(c: DropCondition) {
  return `${label(c.field)} ${OPS.find(([o]) => o === c.op)?.[1] ?? c.op} ${valueText(c.field, c.value)}`;
}

function PreviewText({ p, rule }: { p?: Preview; rule: DropRule }) {
  if (!p || p === "loading") return <span className="faint">counting…</span>;
  if (p === "error") return <span className="faint">can't preview</span>;
  const dropped = p.matched * (1 - rule.keep_percent / 100);
  const share = p.total ? (p.matched / p.total) * 100 : 0;
  return (
    <span>
      {fmtNum(p.matched)} {PRICE_LABEL[rule.signal]} <span className="faint">({share.toFixed(share < 1 ? 1 : 0)}%)</span>
      <div className="faint">saves ~{usd(((dropped * 30) / 1e6) * PRICE[rule.signal].storage)}/month</div>
    </span>
  );
}

function Editor({ rule, maxConditions, busy, preview, onPreview, onChange, onCancel, onSave }: {
  rule: DropRule; maxConditions: number; busy: boolean; preview?: Preview;
  onPreview: () => void; onChange: (r: DropRule) => void; onCancel: () => void; onSave: () => void;
}) {
  const set = (p: Partial<DropRule>) => onChange({ ...rule, ...p });
  const setCond = (i: number, p: Partial<DropCondition>) => set({ conditions: rule.conditions.map((c, j) => (j === i ? { ...c, ...p } : c)) });
  const complete = normalized(rule).conditions.every((c) => (Array.isArray(c.value) ? c.value.length : String(c.value).trim() !== "")
    && !c.field.endsWith("."));
  const fieldOptions = useMemo(() => FIELDS[rule.signal], [rule.signal]);
  return (
    <Panel title={rule.id ? "Edit rule" : "New rule"}>
      <div className="drop-edit">
        <div className="drop-edit-row">
          <label className="field">Name<input className="input" value={rule.name} placeholder="e.g. Debug logs" onChange={(e) => set({ name: e.target.value })} /></label>
          <label className="field">Data
            <select className="select" value={rule.signal}
                    onChange={(e) => set({ signal: e.target.value as Signal, conditions: [{ field: "service", op: "=", value: "" }] })}>
              {SIGNALS.map((s) => <option key={s} value={s}>{SIGNAL_NAME[s]}</option>)}
            </select>
          </label>
          <label className="field">Keep (%)
            <input className="input" type="number" min={0} max={99} style={{ width: 90 }} value={rule.keep_percent}
                   onChange={(e) => set({ keep_percent: Math.max(0, Math.min(99, Number(e.target.value) || 0)) })} />
          </label>
          <span className="faint" style={{ alignSelf: "end", paddingBottom: 6 }}>
            {rule.keep_percent ? `keeps ${rule.keep_percent}% of matching ${PRICE_LABEL[rule.signal]}${rule.signal !== "metrics" ? " (whole traces)" : ""}` : `drops every matching ${PRICE_LABEL[rule.signal].replace(/s$/, "")}`}
          </span>
        </div>
        <div className="faint">When all of these are true:</div>
        {rule.conditions.map((c, i) => <ConditionRow key={i} signal={rule.signal} c={c} fields={fieldOptions} onChange={(p) => setCond(i, p)}
                                                     onRemove={rule.conditions.length > 1 ? () => set({ conditions: rule.conditions.filter((_, j) => j !== i) }) : undefined} />)}
        {rule.conditions.length < maxConditions && (
          <div><button className="linkbtn" onClick={() => set({ conditions: [...rule.conditions, { field: "service", op: "=", value: "" }] })}>+ Add a condition</button></div>
        )}
        <div className="drop-preview">
          {preview && preview !== "loading" && preview !== "error"
            ? <>In the last 24 hours this matched <b>{fmtNum(preview.matched)}</b> of {fmtNum(preview.total)} stored {PRICE_LABEL[rule.signal]}.
                Dropping {rule.keep_percent ? `${100 - rule.keep_percent}% of ` : ""}them saves about{" "}
                <b>{usd(((preview.matched * (1 - rule.keep_percent / 100) * 30) / 1e6) * PRICE[rule.signal].storage)}</b> a month.</>
            : preview === "loading" ? "Counting the last 24 hours…" : preview === "error" ? "Couldn't count: check the conditions." : "See what this rule would drop."}
          {" "}<button className="linkbtn" disabled={!complete || preview === "loading"} onClick={onPreview}>Preview</button>
        </div>
        <div className="drop-edit-row">
          <button className="btn primary" disabled={busy || !complete} onClick={onSave}>{busy ? "Saving…" : "Save rule"}</button>
          <button className="btn ghost" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </Panel>
  );
}

function ConditionRow({ signal, c, fields, onChange, onRemove }: {
  signal: Signal; c: DropCondition; fields: [string, string][]; onChange: (p: Partial<DropCondition>) => void; onRemove?: () => void;
}) {
  const kind = c.field.startsWith("attributes.") ? "attr" : c.field.startsWith("resource.") ? "res" : c.field;
  const key = kind === "attr" ? c.field.slice(11) : kind === "res" ? c.field.slice(9) : "";
  const numeric = NUMERIC.has(c.field);
  const choices = c.field === "severity_number" ? SEVERITIES : c.field === "kind" ? KINDS : c.field === "status_code" ? STATUSES : null;
  const ops = OPS.filter(([o]) => (numeric ? o !== "contains" && o !== "in" : !["<", "<=", ">", ">="].includes(o)));
  return (
    <div className="drop-edit-row">
      <select className="select" value={kind} aria-label="Field" onChange={(e) => {
        const v = e.target.value;
        const field = v === "attr" ? "attributes." : v === "res" ? "resource." : v;
        onChange({ field, op: NUMERIC.has(field) ? "<" : "=", value: NUMERIC.has(field) ? (field === "severity_number" ? 9 : 0) : "" });
      }}>
        {fields.map(([f, l]) => <option key={f} value={f}>{l}</option>)}
        <option value="attr">{signal === "metrics" ? "Data point attribute…" : "Attribute…"}</option>
        <option value="res">Resource attribute…</option>
      </select>
      {(kind === "attr" || kind === "res") && (
        <input className="input mono" placeholder="key, e.g. http.route" value={key} style={{ width: 180 }}
               onChange={(e) => onChange({ field: `${kind === "attr" ? "attributes" : "resource"}.${e.target.value.trim()}` })} />
      )}
      <select className="select" value={c.op} aria-label="Operator" onChange={(e) => {
        const op = e.target.value as DropCondition["op"];
        onChange({ op, value: Array.isArray(c.value) ? c.value.join(", ") : c.value });
      }}>
        {ops.map(([o, l]) => <option key={o} value={o}>{l}</option>)}
      </select>
      {choices ? (
        <select className="select" value={String(c.value)} aria-label="Value" onChange={(e) => onChange({ value: Number(e.target.value) })}>
          {choices.map(([n, l]) => <option key={n} value={n}>{l}</option>)}
        </select>
      ) : (
        <input className="input grow" aria-label="Value" type={numeric ? "number" : "text"}
               placeholder={c.op === "in" ? "comma-separated values" : numeric ? "number" : "value"}
               value={Array.isArray(c.value) ? c.value.join(", ") : c.field === "duration_ns" ? String(Number(c.value) / 1e6) : String(c.value)}
               onChange={(e) => onChange({ value: numeric ? Number(e.target.value) * (c.field === "duration_ns" ? 1e6 : 1) : e.target.value })} />
      )}
      {onRemove && <button className="linkbtn danger" onClick={onRemove} aria-label="Remove condition">✕</button>}
    </div>
  );
}
