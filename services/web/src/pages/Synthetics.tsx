import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { Check, CheckResult, checks, CheckSettings, Constraint, Extraction, records, Step } from "../api";
import type { Ctx } from "../App";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { Stat } from "../components/Stat";
import { TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { fmtTs } from "./Logs";

// Results are the tenant's own telemetry (service "synthetics"): gauges synthetics.check.success /
// .duration / .tls_days_remaining and synthetics.step.duration, and a trace per run (a span per step),
// all with attribute check.id.
const SUCCESS = "synthetics.check.success", DURATION = "synthetics.check.duration";
const TLS = "synthetics.check.tls_days_remaining", STEP_DURATION = "synthetics.step.duration";
const OK = "var(--ok, #3fb68b)";

export function Synthetics({ ctx, path }: { ctx: Ctx; path: string }) {
  const [, , id, sub] = path.split("/");            // /synthetics[/new | /<id>[/edit]]
  if (id === "new") return <CheckForm ctx={ctx} />;
  if (id && sub === "edit") return <EditCheck ctx={ctx} id={id} />;
  if (id) return <CheckDetail ctx={ctx} id={id} />;
  return <CheckList ctx={ctx} />;
}

function useChecks(tick: number) {
  const [state, setState] = useState<{ data: { checks: Check[]; limit: number } | null; error: string | null }>({ data: null, error: null });
  useEffect(() => {
    let live = true;
    checks.list().then((data) => live && setState({ data, error: null }), (e: Error) => live && setState({ data: null, error: e.message }));
    return () => { live = false; };
  }, [tick]);
  return state;
}

// ------------------------------------------------------------------ list

function CheckList({ ctx }: { ctx: Ctx }) {
  const list = useChecks(ctx.tick);
  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const key = ctx.range.key + ctx.tick;
  const byCheck = { signal: "metrics" as const, ...w, services: ["synthetics"], group_by: ["attributes.check.id"], limit: 1000 };
  const uptime = useQuery({ ...byCheck, where: [{ field: "metric_name", op: "=", value: SUCCESS }], aggs: [{ fn: "avg", field: "value" }, { fn: "count" }] }, "u" + key);
  const speed = useQuery({ ...byCheck, where: [{ field: "metric_name", op: "=", value: DURATION }], aggs: [{ fn: "avg", field: "value" }] }, "d" + key);
  const runs = useQuery({ signal: "traces", ...w, services: ["synthetics"], where: [{ field: "attributes.check.result", op: "exists" }],
                          search: { limit: 500 } }, "r" + key);

  const up = new Map((uptime.data ? records(uptime.data) : []).map((r) => [String(r["attributes.check.id"]), r]));
  const ms = new Map((speed.data ? records(speed.data) : []).map((r) => [String(r["attributes.check.id"]), Number(r["avg(value)"])]));
  const last = new Map<string, Record<string, unknown>>();
  for (const r of runs.data ? records(runs.data) : []) {
    const id = String((r.attributes as Record<string, unknown>)?.["check.id"] ?? "");
    if (id && (!last.has(id) || String(r.ts) > String(last.get(id)!.ts))) last.set(id, r);
  }
  const all = list.data?.checks ?? [];
  return (
    <>
      <div className="toolbar">
        <span className="faint">Checks run from us-east-1 (N. Virginia). Their results are part of your data: filter by service <b>synthetics</b> anywhere.</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn primary" disabled={!!list.data && all.length >= list.data.limit} onClick={() => ctx.go("/synthetics/new")}>New check</button>
      </div>
      <Panel title="Synthetic checks" flush right={list.data && <span className="faint">{all.length} of {list.data.limit}</span>}>
        {list.error ? <div className="state error">{list.error}</div>
          : !list.data ? <div className="skeleton" style={{ height: 160 }} />
          : !all.length ? (
            <div className="state" style={{ minHeight: 180, flexDirection: "column", gap: 10 }}>
              <div>No checks yet. A check runs one or more requests against your site or API every few minutes and tells you when they fail or slow down.</div>
              <button className="btn primary" onClick={() => ctx.go("/synthetics/new")}>Create your first check</button>
            </div>
          ) : (
            <RankTable head={["", "check", "first request", "steps", "every", "last run", "uptime", "avg time"]} numCols={2} maxHeight={640}
                       onRow={(i) => ctx.go(`/synthetics/${all[i].id}`)}
                       rows={all.map((c) => {
                         const l = last.get(c.id), u = up.get(c.id);
                         const ok = l ? (l.attributes as Record<string, unknown>)["check.result"] === "pass" : null;
                         return [<Dot ok={c.enabled ? ok : null} title={!c.enabled ? "paused" : ok == null ? "no runs in this range" : ok ? "passing" : "failing"} />,
                                 <span className="link">{c.name} ›</span>, `${c.steps[0]?.method} ${c.steps[0]?.url}`, String(c.steps.length), `${c.frequency} min`,
                                 !c.enabled ? "paused" : l ? fmtTs(String(l.ts)).slice(5, 16) : "—",
                                 u ? pct(Number(u["avg(value)"])) : "—", ms.has(c.id) ? `${fmtNum(ms.get(c.id)!)} ms` : "—"];
                       })} />
          )}
      </Panel>
    </>
  );
}

function Dot({ ok, title }: { ok: boolean | null; title: string }) {
  const color = ok == null ? "var(--text-3)" : ok ? OK : "var(--sev-error)";
  return <span title={title} aria-label={title} style={{ display: "inline-block", width: 9, height: 9, borderRadius: 9, background: color }} />;
}
const pct = (v: number) => `${(v * 100).toFixed(v >= 0.9995 ? 0 : v >= 0.99 ? 2 : 1)}%`;

// ------------------------------------------------------------------ one check

function CheckDetail({ ctx, id }: { ctx: Ctx; id: string }) {
  const [check, setCheck] = useState<Check | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<CheckResult | "running" | null>(null);
  const [nonce, setNonce] = useState(0);
  const load = useCallback(() => checks.get(id).then(setCheck, (e: Error) => setError(e.message)), [id]);
  useEffect(() => { load(); }, [load, ctx.tick]);

  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick, nonce]);
  const b = bucketSeconds(ctx.range), key = id + ctx.range.key + ctx.tick + nonce;
  const mine = (metric: string) => [{ field: "metric_name", op: "=", value: metric }, { field: "attributes.check.id", op: "=", value: id }];
  const base = { signal: "metrics" as const, ...w, services: ["synthetics"] };
  const totals = useQuery({ ...base, where: mine(SUCCESS), aggs: [{ fn: "avg", field: "value" }, { fn: "count" }] }, "t" + key);
  const speed = useQuery({ ...base, where: mine(DURATION), aggs: [{ fn: "avg", field: "value" }, { fn: "p95", field: "value" }] }, "s" + key);
  const tls = useQuery({ ...base, where: mine(TLS), aggs: [{ fn: "min", field: "value" }] }, "c" + key);
  const series = useQuery({ ...base, where: mine(DURATION), group_by: [`ts:${b}`], aggs: [{ fn: "avg", field: "value" }], limit: 10000 }, "d" + key);
  const upSeries = useQuery({ ...base, where: mine(SUCCESS), group_by: [`ts:${b}`], aggs: [{ fn: "avg", field: "value" }], limit: 10000 }, "p" + key);
  const perStep = useQuery({ ...base, where: mine(STEP_DURATION), group_by: ["attributes.step.index"], aggs: [{ fn: "avg", field: "value" }, { fn: "p95", field: "value" }], limit: 20 }, "q" + key);
  const runs = useQuery({ signal: "traces", ...w, services: ["synthetics"], search: { limit: 100 },
                          where: [{ field: "attributes.check.id", op: "=", value: id }, { field: "attributes.check.result", op: "exists" }] }, "r" + key);

  if (error) return <div className="state error">{error}</div>;
  if (!check) return <div className="skeleton" style={{ height: 240 }} />;
  const t = totals.data?.rows[0], s = speed.data?.rows[0];
  const stepTimes = new Map((perStep.data ? records(perStep.data) : []).map((r) => [Number(r["attributes.step.index"]), r]));
  const runNow = async () => {
    setRunning("running");
    try { setRunning((await checks.run(id)).result); setTimeout(() => setNonce((n) => n + 1), 60_000); }   // searchable within ~a minute
    catch (e) { setError((e as Error).message); setRunning(null); }
  };
  const act = async (f: () => Promise<unknown>) => { try { await f(); } catch (e) { setError((e as Error).message); } };
  const points = (q: typeof series, scale = 1) => (q.data ? q.data.rows.map((r) => [Date.parse(String(r[0])), Number(r[1]) * scale] as [number, number]) : []);
  const runRows = runs.data ? records(runs.data).sort((a, c) => String(c.ts).localeCompare(String(a.ts))) : [];
  return (
    <>
      <div className="toolbar">
        <a href="#/synthetics" className="btn" onClick={(e) => { e.preventDefault(); ctx.go("/synthetics"); }}>← All checks</a>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn" disabled={running === "running"} onClick={runNow}>{running === "running" ? "Running…" : "Run now"}</button>
        <button className="btn" onClick={() => act(async () => setCheck(await checks.update(id, { enabled: !check.enabled })))}>{check.enabled ? "Pause" : "Resume"}</button>
        <button className="btn" onClick={() => ctx.go(`/synthetics/${id}/edit`)}>Edit</button>
        <button className="btn" onClick={() => confirm(`Delete the check “${check.name}”? Its past results stay in your data.`) &&
                                               act(async () => { await checks.remove(id); ctx.go("/synthetics"); })}>Delete</button>
      </div>
      {running && running !== "running" && <ResultBox result={running} note="Recorded; it appears in the charts within about a minute." />}
      <Panel title={check.name} right={<span className="faint">{check.steps.length} step{check.steps.length > 1 ? "s" : ""} · every {check.frequency} min{check.enabled ? "" : " · paused"}</span>}>
        <div className="grid">
          <div className="span-3"><Loads q={totals} height={84}>{() => <Stat small value={t && Number(t[1]) ? pct(Number(t[0])) : "—"} sub="uptime" />}</Loads></div>
          <div className="span-3"><Loads q={speed} height={84}>{() => <Stat small value={s && s[0] != null ? `${fmtNum(Number(s[0]))} ms` : "—"} sub="average time (all steps)" />}</Loads></div>
          <div className="span-3"><Loads q={speed} height={84}>{() => <Stat small value={s && s[1] != null ? `${fmtNum(Number(s[1]))} ms` : "—"} sub="p95 time (all steps)" />}</Loads></div>
          <div className="span-3"><Loads q={tls} height={84}>{() => {
            const d = tls.data?.rows[0]?.[0];
            return <Stat small value={d == null ? "—" : `${Math.floor(Number(d))} days`} sub="until a TLS certificate expires" />;
          }}</Loads></div>
        </div>
      </Panel>
      <div className="grid">
        <Panel title="Time for all steps" span={8}>
          <Loads q={series} empty={!series.data?.rows.length} height={200}>
            {() => <TimeSeries series={[{ label: "average", color: "var(--series-1)", points: points(series) }]} range={ctx.range} unit=" ms" height={200} area={false} />}
          </Loads>
        </Panel>
        <Panel title="Passing runs" span={4}>
          <Loads q={upSeries} empty={!upSeries.data?.rows.length} height={200}>
            {() => <TimeSeries series={[{ label: "% passing", color: OK, points: points(upSeries, 100) }]} range={ctx.range} unit="%" height={200} />}
          </Loads>
        </Panel>
      </div>
      <Panel title="Steps" flush>
        <RankTable head={["#", "step", "request", "checks", "avg", "p95"]} numCols={2}
                   rows={check.steps.map((st, i) => {
                     const r = stepTimes.get(i + 1);
                     return [String(i + 1), st.name, `${st.method} ${st.url}`,
                             [st.auth.type !== "none" ? st.auth.type : "", ...st.constraints.map(describeConstraint), ...st.extract.map((e) => `→ {${e.name}}`)].filter(Boolean).join(" · "),
                             r ? `${fmtNum(Number(r["avg(value)"]))} ms` : "—", r ? `${fmtNum(Number(r["p95(value)"]))} ms` : "—"];
                   })} />
      </Panel>
      <Panel title="Recent runs" flush right={<span className="faint">click one to open its trace (a span per step)</span>}>
        <Loads q={runs} empty={!runRows.length} height={120}>
          {() => <RankTable head={["time", "result", "failed at", "why", "time"]} maxHeight={420}
                            onRow={(i) => ctx.go(`/traces/${runRows[i].trace_id}`)}
                            rows={runRows.map((r) => {
                              const a = r.attributes as Record<string, unknown>, pass = a["check.result"] === "pass";
                              const why = String(a["check.failure"] ?? "");
                              return [fmtTs(String(r.ts)), <span style={{ color: pass ? OK : "var(--sev-error)" }}>{pass ? "passed" : "failed"}</span>,
                                      pass ? "" : (check.steps[Number(a["check.failed_step"]) - 1]?.name ?? "—"), why.replace(/^[^:]*: /, ""),
                                      a["check.total_ms"] != null ? `${fmtNum(Number(a["check.total_ms"]))} ms` : "—"];
                            })} />}
        </Loads>
      </Panel>
    </>
  );
}

const OPS: Record<string, string> = { equals: "=", not_equals: "≠", contains: "contains", lt: "<", gt: ">", exists: "exists" };
function describeConstraint(c: Constraint): string {
  switch (c.type) {
    case "status": return `status ${c.expr}`;
    case "max_ms": return `≤ ${c.value} ms`;
    case "body_contains": return `body has “${c.value}”`;
    case "body_not_contains": return `body lacks “${c.value}”`;
    case "body_regex": return `body ~ /${c.value}/`;
    case "header": return `${c.name} ${OPS[c.op ?? "exists"]}${c.op !== "exists" ? ` “${c.value}”` : ""}`;
    case "json": return `${c.path} ${OPS[c.op ?? "exists"]}${c.op !== "exists" ? ` ${c.value}` : ""}`;
    case "tls_days": return `cert ≥ ${c.value} days`;
    default: return c.type;
  }
}

// ------------------------------------------------------------------ create / edit

const newStep = (n: number): Step => ({
  name: `Step ${n}`, method: "GET", url: "https://", headers: {}, auth: { type: "none" }, follow_redirects: true,
  verify_tls: true, record_body: true, extract: [], constraints: [{ type: "status", expr: "<400" }],
});
const EMPTY: CheckSettings = { name: "", frequency: 5, timeout_ms: 20000, enabled: true, variables: {}, steps: [newStep(1)] };

function EditCheck({ ctx, id }: { ctx: Ctx; id: string }) {
  const [check, setCheck] = useState<Check | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { checks.get(id).then(setCheck, (e: Error) => setError(e.message)); }, [id]);
  if (error) return <div className="state error">{error}</div>;
  return check ? <CheckForm ctx={ctx} existing={check} /> : <div className="skeleton" style={{ height: 240 }} />;
}

type SecretRow = { name: string; saved: boolean; value: string; remove: boolean };
const SECRET_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,39}$/;

function CheckForm({ ctx, existing }: { ctx: Ctx; existing?: Check }) {
  const [c, setC] = useState<CheckSettings>(() => existing ? { ...existing } : EMPTY);
  const [vars, setVars] = useState<[string, string][]>(Object.entries(existing?.variables ?? {}));
  const [secretRows, setSecretRows] = useState<SecretRow[]>((existing?.secret_names ?? []).map((name) => ({ name, saved: true, value: "", remove: false })));
  const [open, setOpen] = useState(0);
  const [result, setResult] = useState<CheckResult | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<CheckSettings>) => { setC({ ...c, ...patch }); setResult(null); };
  const setStep = (i: number, s: Step) => set({ steps: c.steps.map((x, j) => (j === i ? s : x)) });
  const moveStep = (i: number, d: number) => { const s = [...c.steps]; [s[i], s[i + d]] = [s[i + d], s[i]]; set({ steps: s }); setOpen(i + d); };

  const secretNames = secretRows.filter((r) => !r.remove && r.name && (r.saved || r.value)).map((r) => r.name);
  const settings = (): CheckSettings => {
    const secrets: Record<string, string | null> = {};
    for (const r of secretRows) {
      if (r.remove && r.saved) secrets[r.name] = null;
      else if (!r.remove && r.name && r.value) secrets[r.name] = r.value;
    }
    const steps = c.steps.map((s) => ({ ...s, body: s.body && !["GET", "HEAD"].includes(s.method) ? s.body : undefined }));
    return { ...c, name: c.name || steps[0].name, variables: Object.fromEntries(vars.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v])), steps, secrets };
  };
  const run = async (what: "test" | "save") => {
    setBusy(what); setError(null);
    const bad = secretRows.find((r) => !r.remove && r.name && !SECRET_NAME.test(r.name));
    if (bad) { setBusy(null); return setError(`Secret name “${bad.name}”: letters, digits and _, starting with a letter or _.`); }
    try {
      if (what === "test") setResult((await checks.test({ ...settings(), ...(existing ? { id: existing.id } : {}) })).result);
      else {
        const saved = existing ? await checks.update(existing.id, settings()) : await checks.create(settings());
        ctx.go(`/synthetics/${saved.id}`);
      }
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  };
  const submit = (e: FormEvent) => { e.preventDefault(); run("save"); };
  const hiddenBefore = (i: number) => [...secretNames, ...c.steps.slice(0, i).flatMap((s) => s.extract.map((x) => x.name))];
  const varsBefore = (i: number) => [...vars.map(([k]) => k).filter(Boolean), ...hiddenBefore(i)];

  return (
    <form onSubmit={submit}>
      <div className="toolbar">
        <a href="#/synthetics" className="btn" onClick={(e) => { e.preventDefault(); ctx.go(existing ? `/synthetics/${existing.id}` : "/synthetics"); }}>← Cancel</a>
      </div>
      <Panel title={existing ? `Edit “${existing.name}”` : "New check"}>
        <Section title="Basic configuration">
          <div className="form-grid">
            <label>Name<input className="input" required value={c.name} maxLength={80} onChange={(e) => set({ name: e.target.value })} placeholder="Checkout API" /></label>
            <label>Run every
              <select className="select" value={c.frequency} onChange={(e) => set({ frequency: Number(e.target.value) })}>
                {[1, 5, 15].map((f) => <option key={f} value={f}>{f} minute{f > 1 ? "s" : ""}</option>)}
              </select>
            </label>
            <label>Give up after (whole check, ms)
              <input className="input" type="number" min={1000} max={25000} step={500} value={c.timeout_ms} onChange={(e) => set({ timeout_ms: Number(e.target.value) })} />
            </label>
          </div>
        </Section>

        <Section title="Variables" hint="Use them as {name} in any step's URL, headers, body or constraints.">
          <Rows rows={vars} add={() => setVars([...vars, ["", ""]])} addLabel="Variable" max={20}
                render={([k, v], i) => (<>
                  <input className="input mono" placeholder="name" value={k} onChange={(e) => setVars(vars.map((x, j) => (j === i ? [e.target.value, v] : x)))} />
                  <input className="input mono grow" placeholder="value, e.g. https://api.example.com" value={v} onChange={(e) => setVars(vars.map((x, j) => (j === i ? [k, e.target.value] : x)))} />
                  <Remove onClick={() => setVars(vars.filter((_, j) => j !== i))} />
                </>)} />
        </Section>

        <Section title="Secrets" hint="Passwords, tokens and keys. Stored encrypted; never shown again or recorded in results. Use as {name}.">
          <Rows rows={secretRows} add={() => setSecretRows([...secretRows, { name: "", saved: false, value: "", remove: false }])} addLabel="Secret" max={20}
                render={(r, i) => {
                  const upd = (p: Partial<SecretRow>) => setSecretRows(secretRows.map((x, j) => (j === i ? { ...x, ...p } : x)));
                  return (<>
                    <input className="input mono" placeholder="name" value={r.name} disabled={r.saved} onChange={(e) => upd({ name: e.target.value })} />
                    <input className="input mono grow" type="password" autoComplete="new-password" disabled={r.remove}
                           placeholder={r.saved ? (r.remove ? "will be removed" : "•••••••• saved (type to replace)") : "value"}
                           value={r.value} onChange={(e) => upd({ value: e.target.value })} />
                    {r.saved ? <button type="button" className="btn" onClick={() => upd({ remove: !r.remove, value: "" })}>{r.remove ? "Keep" : "Remove"}</button>
                             : <Remove onClick={() => setSecretRows(secretRows.filter((_, j) => j !== i))} />}
                  </>);
                }} />
        </Section>

        <Section title={`Steps (${c.steps.length} of 10)`} hint="Run in order; the first step that fails ends the run. Cookies carry over between steps.">
          {c.steps.map((s, i) => (
            <StepCard key={i} step={s} index={i} count={c.steps.length} open={open === i} onToggle={() => setOpen(open === i ? -1 : i)}
                      onChange={(x) => setStep(i, x)} onMove={(d) => moveStep(i, d)} onRemove={() => { set({ steps: c.steps.filter((_, j) => j !== i) }); setOpen(-1); }}
                      vars={varsBefore(i)} hidden={hiddenBefore(i)} />
          ))}
          {c.steps.length < 10 && (
            <button type="button" className="btn" onClick={() => { set({ steps: [...c.steps, newStep(c.steps.length + 1)] }); setOpen(c.steps.length); }}>+ Step</button>
          )}
        </Section>

        {error && <div className="form-error" role="alert" style={{ marginTop: 10 }}>{error}</div>}
        {result && <ResultBox result={result} note="Test runs are not recorded." />}
        <div className="toolbar" style={{ marginTop: 14 }}>
          <button type="button" className="btn" disabled={!!busy} onClick={() => run("test")}>{busy === "test" ? "Testing…" : "Test"}</button>
          <button className="btn primary" disabled={!!busy}>{busy === "save" ? "Saving…" : existing ? "Save" : "Create check"}</button>
        </div>
      </Panel>
    </form>
  );
}

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="form-section">
      <h3>{title}</h3>
      {hint && <div className="faint" style={{ marginBottom: 8 }}>{hint}</div>}
      {children}
    </section>
  );
}

function Rows<T>({ rows, render, add, addLabel, max }: { rows: T[]; render: (r: T, i: number) => ReactNode; add: () => void; addLabel: string; max: number }) {
  return (
    <div>
      {rows.map((r, i) => <div key={i} className="toolbar" style={{ marginBottom: 6 }}>{render(r, i)}</div>)}
      {rows.length < max && <button type="button" className="btn" onClick={add}>+ {addLabel}</button>}
    </div>
  );
}

const Remove = ({ onClick }: { onClick: () => void }) => <button type="button" className="btn" onClick={onClick} aria-label="Remove">✕</button>;

const CONSTRAINT_TYPES: [string, string][] = [
  ["status", "Response status code"], ["max_ms", "Response time"], ["body_contains", "Body contains"], ["body_not_contains", "Body does not contain"],
  ["body_regex", "Body matches regex"], ["header", "Response header"], ["json", "JSON value"], ["tls_days", "TLS certificate valid for"],
];

function StepCard(p: { step: Step; index: number; count: number; open: boolean; onToggle: () => void; onChange: (s: Step) => void;
                       onMove: (d: number) => void; onRemove: () => void; vars: string[]; hidden: string[] }) {
  const s = p.step, set = (patch: Partial<Step>) => p.onChange({ ...s, ...patch });
  const headers = Object.entries(s.headers);
  const setHeaders = (h: [string, string][]) => set({ headers: Object.fromEntries(h) });
  const withBody = !["GET", "HEAD"].includes(s.method);
  const pick = (value: string | undefined, onPick: (v: string) => void, what: string) => (
    <select className="select" value={value ?? ""} onChange={(e) => onPick(e.target.value)}>
      <option value="">Choose a secret or earlier value…</option>
      {p.hidden.map((h) => <option key={h} value={`{${h}}`}>{`{${h}}`}</option>)}
      {!p.hidden.length && <option disabled>Add a secret above for the {what}</option>}
    </select>
  );
  return (
    <div className={`step-card${p.open ? " open" : ""}`}>
      <div className="step-head" onClick={p.onToggle} role="button" aria-expanded={p.open}>
        <span className="step-num">{p.index + 1}</span>
        <b>{s.name}</b>
        <span className="faint mono step-url">{s.method} {s.url}</span>
        <span className="spacer" style={{ flex: 1 }} />
        <span onClick={(e) => e.stopPropagation()} className="step-tools">
          <button type="button" className="btn" disabled={p.index === 0} onClick={() => p.onMove(-1)} aria-label="Move up">↑</button>
          <button type="button" className="btn" disabled={p.index === p.count - 1} onClick={() => p.onMove(1)} aria-label="Move down">↓</button>
          <button type="button" className="btn" disabled={p.count === 1} onClick={p.onRemove} aria-label="Remove step">✕</button>
        </span>
      </div>
      {p.open && (
        <div className="step-body">
          <div className="form-grid">
            <label>Step name<input className="input" value={s.name} maxLength={80} onChange={(e) => set({ name: e.target.value })} /></label>
            <label>HTTP method
              <select className="select" value={s.method} onChange={(e) => set({ method: e.target.value })}>
                {["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].map((m) => <option key={m}>{m}</option>)}
              </select>
            </label>
            <label>User agent<input className="input" value={s.user_agent ?? ""} placeholder="Leasyd-Synthetics/1.0 (default)" onChange={(e) => set({ user_agent: e.target.value || undefined })} /></label>
            <label className="wide">HTTP request URL
              <input className="input mono" required value={s.url} onChange={(e) => set({ url: e.target.value })} placeholder="https://www.mysite.com:8080/api/orders/{order_id}" />
              <span className="faint">Variables in braces, e.g. {"{base}"}/orders/{"{order_id}"}{p.vars.length ? `: ${p.vars.map((v) => `{${v}}`).join(" ")}` : ""}. Must be a public address.</span>
            </label>
          </div>

          <h4>Authentication</h4>
          <div className="form-grid">
            <label>Type
              <select className="select" value={s.auth.type} onChange={(e) => set({ auth: { type: e.target.value as Step["auth"]["type"] } })}>
                <option value="none">None</option><option value="basic">Basic (username and password)</option><option value="bearer">Bearer token</option>
              </select>
            </label>
            {s.auth.type === "basic" && (<>
              <label>Username<input className="input" value={s.auth.username ?? ""} onChange={(e) => set({ auth: { ...s.auth, username: e.target.value } })} /></label>
              <label>Password{pick(s.auth.password, (v) => set({ auth: { ...s.auth, password: v } }), "password")}</label>
            </>)}
            {s.auth.type === "bearer" && <label>Token{pick(s.auth.token, (v) => set({ auth: { ...s.auth, token: v } }), "token")}</label>}
          </div>

          <h4>Request</h4>
          <Rows rows={headers} add={() => setHeaders([...headers, ["", ""]])} addLabel="Header" max={20}
                render={([k, v], i) => (<>
                  <input className="input mono" placeholder="Header" value={k} onChange={(e) => setHeaders(headers.map((x, j) => (j === i ? [e.target.value, v] : x)))} />
                  <input className="input mono grow" placeholder="value (variables allowed)" value={v} onChange={(e) => setHeaders(headers.map((x, j) => (j === i ? [k, e.target.value] : x)))} />
                  <Remove onClick={() => setHeaders(headers.filter((_, j) => j !== i))} />
                </>)} />
          {withBody && (
            <label className="block">Request body
              <textarea className="input mono" rows={4} maxLength={16384} value={s.body ?? ""} placeholder='{"email": "{email}"}' onChange={(e) => set({ body: e.target.value })} />
            </label>
          )}
          <div className="checks">
            <label className="check"><input type="checkbox" checked={s.follow_redirects} onChange={(e) => set({ follow_redirects: e.target.checked })} /> Follow redirects</label>
            <label className="check"><input type="checkbox" checked={!s.verify_tls} onChange={(e) => set({ verify_tls: !e.target.checked })} /> Accept any SSL certificate</label>
            <label className="check"><input type="checkbox" checked={!s.record_body} onChange={(e) => set({ record_body: !e.target.checked })} /> Sensitive: don't record the response when it fails</label>
          </div>

          <h4>Extract variables for later steps</h4>
          <Rows rows={s.extract} add={() => set({ extract: [...s.extract, { name: "", from: "json", expr: "" }] })} addLabel="Extract" max={10}
                render={(x, i) => {
                  const upd = (patch: Partial<Extraction>) => set({ extract: s.extract.map((e, j) => (j === i ? { ...e, ...patch } : e)) });
                  return (<>
                    <input className="input mono" placeholder="variable" value={x.name} onChange={(e) => upd({ name: e.target.value })} />
                    <select className="select" value={x.from} onChange={(e) => upd({ from: e.target.value as Extraction["from"] })}>
                      <option value="json">from JSON path</option><option value="regex">from regex (1st group)</option><option value="header">from header</option>
                    </select>
                    <input className="input mono grow" value={x.expr} onChange={(e) => upd({ expr: e.target.value })}
                           placeholder={x.from === "json" ? "data.items[0].id" : x.from === "regex" ? 'id="(\\d+)"' : "X-Request-Id"} />
                    <Remove onClick={() => set({ extract: s.extract.filter((_, j) => j !== i) })} />
                  </>);
                }} />

          <h4>Constraints</h4>
          <Rows rows={s.constraints} add={() => set({ constraints: [...s.constraints, { type: "status", expr: "<400" }] })} addLabel="Constraint" max={20}
                render={(k, i) => <ConstraintRow c={k} onChange={(x) => set({ constraints: s.constraints.map((e, j) => (j === i ? x : e)) })}
                                                 onRemove={() => set({ constraints: s.constraints.filter((_, j) => j !== i) })} />} />
          <div className="faint">With no constraints a step passes when the status is below 400.</div>
        </div>
      )}
    </div>
  );
}

function ConstraintRow({ c, onChange, onRemove }: { c: Constraint; onChange: (c: Constraint) => void; onRemove: () => void }) {
  const set = (patch: Partial<Constraint>) => onChange({ ...c, ...patch });
  const ops = (list: string[]) => (
    <select className="select" value={c.op ?? "exists"} onChange={(e) => set({ op: e.target.value })}>
      {list.map((o) => <option key={o} value={o}>{OPS[o]}</option>)}
    </select>
  );
  return (<>
    <select className="select" value={c.type} onChange={(e) => onChange({ type: e.target.value, ...({ status: { expr: "<400" }, header: { op: "exists" }, json: { op: "exists" } } as Record<string, object>)[e.target.value] })}>
      {CONSTRAINT_TYPES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
    </select>
    {c.type === "status" && <input className="input mono grow" value={c.expr ?? ""} onChange={(e) => set({ expr: e.target.value })}
                                   placeholder="<400 · 2xx · 200,204 · 406-410 · >=500" title="Exact number, <, <=, >, >=, !=, a range, or a class like 2xx; separate several with commas (any may match)." />}
    {(c.type === "max_ms" || c.type === "tls_days") && (<>
      <input className="input" type="number" min={1} value={c.value ?? ""} onChange={(e) => set({ value: Number(e.target.value) })} />
      <span className="faint">{c.type === "max_ms" ? "ms at most" : "days at least"}</span>
    </>)}
    {["body_contains", "body_not_contains", "body_regex"].includes(c.type) && (
      <input className="input mono grow" value={String(c.value ?? "")} onChange={(e) => set({ value: e.target.value })} placeholder={c.type === "body_regex" ? '"status":\\s*"ok"' : "text (variables allowed)"} />
    )}
    {c.type === "header" && (<>
      <input className="input mono" placeholder="Header" value={c.name ?? ""} onChange={(e) => set({ name: e.target.value })} />
      {ops(["exists", "equals", "contains"])}
      {c.op !== "exists" && <input className="input mono grow" placeholder="value" value={String(c.value ?? "")} onChange={(e) => set({ value: e.target.value })} />}
    </>)}
    {c.type === "json" && (<>
      <input className="input mono" placeholder="order.status" value={c.path ?? ""} onChange={(e) => set({ path: e.target.value })} />
      {ops(["exists", "equals", "not_equals", "contains", "lt", "gt"])}
      {c.op !== "exists" && <input className="input mono grow" placeholder="value" value={String(c.value ?? "")} onChange={(e) => set({ value: e.target.value })} />}
    </>)}
    <Remove onClick={onRemove} />
  </>);
}

function ResultBox({ result, note }: { result: CheckResult; note: string }) {
  return (
    <div className={`result-box ${result.ok ? "pass" : "fail"}`} role="status">
      <b>{result.ok ? "Passed" : "Failed"}</b> <span className="faint">· {fmtNum(result.total_ms)} ms{result.tls_days != null ? ` · certificate valid for ${Math.floor(result.tls_days)} more days` : ""}</span>
      <ol className="result-steps">
        {result.steps.map((s, i) => {
          const t = s.timings, parts: [string, number | undefined][] = [["DNS", t.dns_ms], ["connect", t.connect_ms], ["TLS", t.tls_ms], ["first byte", t.ttfb_ms], ["total", t.total_ms]];
          return (
            <li key={i}>
              <span style={{ color: s.ok ? OK : "var(--sev-error)" }}>{s.ok ? "✓" : "✕"}</span> <b>{s.name}</b>
              {s.status != null && <span className="mono"> · HTTP {s.status}</span>}
              {s.failure && <span> · {s.failure}</span>}
              {s.extracted.length > 0 && <span className="faint"> · set {s.extracted.map((x) => `{${x}}`).join(" ")}</span>}
              <div className="faint mono">{parts.filter(([, v]) => v != null).map(([k, v]) => `${k} ${fmtNum(v!)} ms`).join(" · ")}</div>
              {s.body_sample && <pre className="body-sample">{s.body_sample}</pre>}
            </li>
          );
        })}
      </ol>
      <div className="faint">{note}</div>
    </div>
  );
}
