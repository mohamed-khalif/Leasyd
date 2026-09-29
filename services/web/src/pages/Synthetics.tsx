import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Check, CheckResult, checks, CheckSettings, records } from "../api";
import type { Ctx } from "../App";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { Stat } from "../components/Stat";
import { TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { fmtTs } from "./Logs";

// Results are the tenant's own telemetry (service "synthetics"): gauges synthetics.check.success /
// .duration / .tls_days_remaining and one span per run, all with attribute check.id.
const SUCCESS = "synthetics.check.success", DURATION = "synthetics.check.duration", TLS = "synthetics.check.tls_days_remaining";

export function Synthetics({ ctx, path }: { ctx: Ctx; path: string }) {
  const [, , id, sub] = path.split("/");            // /synthetics[/new | /<id>[/edit]]
  if (id === "new") return <CheckForm ctx={ctx} />;
  if (id && sub === "edit") return <EditCheck ctx={ctx} id={id} />;
  if (id) return <CheckDetail ctx={ctx} id={id} />;
  return <CheckList ctx={ctx} />;
}

/** The tenant's checks (a fetch with loading/error state, refreshed by `tick`). */
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
  const runs = useQuery({ signal: "traces", ...w, services: ["synthetics"], search: { limit: 500 } }, "r" + key);

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
        <span className="faint">Checks run from us-east-1 (N. Virginia) and their results are part of your data: filter by service <b>synthetics</b> anywhere.</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn primary" disabled={!!list.data && all.length >= list.data.limit} onClick={() => ctx.go("/synthetics/new")}>New check</button>
      </div>
      <Panel title="Synthetic checks" flush right={list.data && <span className="faint">{all.length} of {list.data.limit}</span>}>
        {list.error ? <div className="state error">{list.error}</div>
          : !list.data ? <div className="skeleton" style={{ height: 160 }} />
          : !all.length ? (
            <div className="state" style={{ minHeight: 180, flexDirection: "column", gap: 10 }}>
              <div>No checks yet. A check requests a URL of yours every few minutes and tells you when it fails or slows down.</div>
              <button className="btn primary" onClick={() => ctx.go("/synthetics/new")}>Create your first check</button>
            </div>
          ) : (
            <RankTable head={["", "check", "url", "every", "last run", "uptime", "avg response"]} numCols={2} maxHeight={640}
                       onRow={(i) => ctx.go(`/synthetics/${all[i].id}`)}
                       rows={all.map((c) => {
                         const l = last.get(c.id), u = up.get(c.id);
                         const ok = l ? (l.attributes as Record<string, unknown>)["check.result"] === "pass" : null;
                         return [<Dot ok={c.enabled ? ok : null} title={!c.enabled ? "paused" : ok == null ? "no runs in this range" : ok ? "passing" : "failing"} />,
                                 <span className="link">{c.name} ›</span>, c.url, `${c.frequency} min`,
                                 !c.enabled ? "paused" : l ? fmtTs(String(l.ts)).slice(5, 16) : "—",
                                 u ? pct(Number(u["avg(value)"])) : "—", ms.has(c.id) ? `${fmtNum(ms.get(c.id)!)} ms` : "—"];
                       })} />
          )}
      </Panel>
    </>
  );
}

function Dot({ ok, title }: { ok: boolean | null; title: string }) {
  const color = ok == null ? "var(--text-3)" : ok ? "var(--ok, #3fb68b)" : "var(--sev-error)";
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
  const runs = useQuery({ signal: "traces", ...w, services: ["synthetics"], where: [{ field: "attributes.check.id", op: "=", value: id }],
                          search: { limit: 100 } }, "r" + key);

  if (error) return <div className="state error">{error}</div>;
  if (!check) return <div className="skeleton" style={{ height: 240 }} />;
  const t = totals.data?.rows[0], s = speed.data?.rows[0];
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
      <Panel title={check.name} right={<span className="faint mono">{check.method} {check.url} · every {check.frequency} min{check.enabled ? "" : " · paused"}</span>}>
        <div className="grid">
          <div className="span-3"><Loads q={totals} height={84}>{() => <Stat small value={t && Number(t[1]) ? pct(Number(t[0])) : "—"} sub="uptime" />}</Loads></div>
          <div className="span-3"><Loads q={speed} height={84}>{() => <Stat small value={s && s[0] != null ? `${fmtNum(Number(s[0]))} ms` : "—"} sub="average response" />}</Loads></div>
          <div className="span-3"><Loads q={speed} height={84}>{() => <Stat small value={s && s[1] != null ? `${fmtNum(Number(s[1]))} ms` : "—"} sub="p95 response" />}</Loads></div>
          <div className="span-3"><Loads q={tls} height={84}>{() => {
            const d = tls.data?.rows[0]?.[0];
            return <Stat small value={d == null ? "—" : `${Math.floor(Number(d))} days`} sub="until the TLS certificate expires" />;
          }}</Loads></div>
        </div>
      </Panel>
      <div className="grid">
        <Panel title="Response time" span={8}>
          <Loads q={series} empty={!series.data?.rows.length} height={200}>
            {() => <TimeSeries series={[{ label: "average", color: "var(--series-1)", points: points(series) }]} range={ctx.range} unit=" ms" height={200} area={false} />}
          </Loads>
        </Panel>
        <Panel title="Passing runs" span={4}>
          <Loads q={upSeries} empty={!upSeries.data?.rows.length} height={200}>
            {() => <TimeSeries series={[{ label: "% passing", color: "var(--ok, #3fb68b)", points: points(upSeries, 100) }]} range={ctx.range} unit="%" height={200} />}
          </Loads>
        </Panel>
      </div>
      <Panel title="Recent runs" flush right={<span className="faint">click one to open its trace</span>}>
        <Loads q={runs} empty={!runRows.length} height={120}>
          {() => <RankTable head={["time", "result", "status", "why it failed", "response"]} maxHeight={420}
                            onRow={(i) => ctx.go(`/traces/${runRows[i].trace_id}`)}
                            rows={runRows.map((r) => {
                              const a = r.attributes as Record<string, unknown>, pass = a["check.result"] === "pass";
                              return [fmtTs(String(r.ts)), <span style={{ color: pass ? "var(--ok, #3fb68b)" : "var(--sev-error)" }}>{pass ? "passed" : "failed"}</span>,
                                      String(a["http.response.status_code"] ?? "—"), String(a["check.failure"] ?? ""),
                                      a["check.total_ms"] != null ? `${fmtNum(Number(a["check.total_ms"]))} ms` : "—"];
                            })} />}
        </Loads>
      </Panel>
    </>
  );
}

// ------------------------------------------------------------------ create / edit

const EMPTY: CheckSettings = { name: "", url: "https://", method: "GET", frequency: 5, timeout_ms: 10000, headers: {},
                               expect: { status: "2xx" }, follow_redirects: true, enabled: true };

function EditCheck({ ctx, id }: { ctx: Ctx; id: string }) {
  const [check, setCheck] = useState<Check | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { checks.get(id).then(setCheck, (e: Error) => setError(e.message)); }, [id]);
  if (error) return <div className="state error">{error}</div>;
  return check ? <CheckForm ctx={ctx} existing={check} /> : <div className="skeleton" style={{ height: 240 }} />;
}

function CheckForm({ ctx, existing }: { ctx: Ctx; existing?: Check }) {
  const [c, setC] = useState<CheckSettings>(existing ?? EMPTY);
  const [headers, setHeaders] = useState<[string, string][]>(Object.entries(existing?.headers ?? {}));
  const [result, setResult] = useState<CheckResult | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<CheckSettings>) => { setC({ ...c, ...patch }); setResult(null); };
  const setExpect = (patch: Partial<CheckSettings["expect"]>) => set({ expect: { ...c.expect, ...patch } });
  const settings = (): CheckSettings => {
    const expect = { status: c.expect.status || "2xx", ...(c.expect.max_ms ? { max_ms: Number(c.expect.max_ms) } : {}),
                     ...(c.expect.contains ? { contains: c.expect.contains } : {}) };
    const withBody = c.method === "POST" || c.method === "PUT";
    return { ...c, name: c.name || c.url.replace(/^https?:\/\//, "").slice(0, 80), expect,
             headers: Object.fromEntries(headers.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v])),
             body: withBody && c.body ? c.body : undefined };
  };
  const run = async (what: "test" | "save") => {
    setBusy(what); setError(null);
    try {
      if (what === "test") setResult((await checks.test(settings())).result);
      else {
        const saved = existing ? await checks.update(existing.id, settings()) : await checks.create(settings());
        ctx.go(`/synthetics/${saved.id}`);
      }
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  };
  const submit = (e: FormEvent) => { e.preventDefault(); run("save"); };
  return (
    <form onSubmit={submit}>
      <div className="toolbar">
        <a href="#/synthetics" className="btn" onClick={(e) => { e.preventDefault(); ctx.go(existing ? `/synthetics/${existing.id}` : "/synthetics"); }}>← Cancel</a>
      </div>
      <Panel title={existing ? `Edit “${existing.name}”` : "New check"}>
        <div className="form-grid">
          <label className="wide">URL
            <input className="input mono" required value={c.url} onChange={(e) => set({ url: e.target.value })} placeholder="https://api.example.com/health" />
            <span className="faint">Must be reachable from the internet. Private and internal addresses are refused.</span>
          </label>
          <label>Name<input className="input" value={c.name} maxLength={80} onChange={(e) => set({ name: e.target.value })} placeholder="Homepage" /></label>
          <label>Method
            <select className="select" value={c.method} onChange={(e) => set({ method: e.target.value })}>
              {["GET", "HEAD", "POST", "PUT", "OPTIONS"].map((m) => <option key={m}>{m}</option>)}
            </select>
          </label>
          <label>Run every
            <select className="select" value={c.frequency} onChange={(e) => set({ frequency: Number(e.target.value) })}>
              {[1, 5, 15].map((f) => <option key={f} value={f}>{f} minute{f > 1 ? "s" : ""}</option>)}
            </select>
          </label>
          <label>Give up after (ms)
            <input className="input" type="number" min={1000} max={20000} step={500} value={c.timeout_ms} onChange={(e) => set({ timeout_ms: Number(e.target.value) })} />
          </label>
          <label>Passes if the status is
            <input className="input mono" value={c.expect.status} onChange={(e) => setExpect({ status: e.target.value })} placeholder="2xx" />
            <span className="faint">e.g. 2xx, 200, or 200,204,3xx</span>
          </label>
          <label>…and it answers within (ms)
            <input className="input" type="number" min={1} max={20000} value={c.expect.max_ms ?? ""} placeholder="any time"
                   onChange={(e) => setExpect({ max_ms: e.target.value ? Number(e.target.value) : undefined })} />
          </label>
          <label>…and the response contains
            <input className="input" value={c.expect.contains ?? ""} maxLength={200} placeholder="any text"
                   onChange={(e) => setExpect({ contains: e.target.value || undefined })} />
          </label>
          <label className="check"><input type="checkbox" checked={c.follow_redirects} onChange={(e) => set({ follow_redirects: e.target.checked })} /> Follow redirects</label>
          <div className="wide">
            <div className="faint" style={{ marginBottom: 6 }}>Request headers (optional, at most 10; not for passwords or tokens yet)</div>
            {headers.map(([k, v], i) => (
              <div key={i} className="toolbar" style={{ marginBottom: 6 }}>
                <input className="input mono" placeholder="Header" value={k} onChange={(e) => { const h = [...headers]; h[i] = [e.target.value, v]; setHeaders(h); }} />
                <input className="input mono grow" placeholder="value" value={v} onChange={(e) => { const h = [...headers]; h[i] = [k, e.target.value]; setHeaders(h); }} />
                <button type="button" className="btn" onClick={() => setHeaders(headers.filter((_, j) => j !== i))} aria-label="Remove header">✕</button>
              </div>
            ))}
            {headers.length < 10 && <button type="button" className="btn" onClick={() => setHeaders([...headers, ["", ""]])}>Add header</button>}
          </div>
          {(c.method === "POST" || c.method === "PUT") && (
            <label className="wide">Request body
              <textarea className="input mono" rows={4} maxLength={16384} value={c.body ?? ""} onChange={(e) => set({ body: e.target.value })} />
            </label>
          )}
        </div>
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

function ResultBox({ result, note }: { result: CheckResult; note: string }) {
  const t = result.timings, steps: [string, number | undefined][] = [["DNS", t.dns_ms], ["connect", t.connect_ms], ["TLS", t.tls_ms], ["first byte", t.ttfb_ms], ["total", t.total_ms]];
  return (
    <div className={`result-box ${result.ok ? "pass" : "fail"}`} role="status">
      <b>{result.ok ? "Passed" : "Failed"}</b>
      {result.status != null && <span className="mono"> · HTTP {result.status}</span>}
      {result.failure && <span> · {result.failure}</span>}
      <div className="faint mono" style={{ marginTop: 4 }}>
        {steps.filter(([, v]) => v != null).map(([k, v]) => `${k} ${fmtNum(v!)} ms`).join(" · ")}
        {result.tls_days != null && ` · certificate valid for ${Math.floor(result.tls_days)} more days`}
      </div>
      <div className="faint" style={{ marginTop: 4 }}>{note}</div>
    </div>
  );
}
