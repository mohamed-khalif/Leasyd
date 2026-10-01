// One synthetic check: Overview (configuration, uptime, timings), Check runs (every run, with a
// side panel showing the run's steps, requests and responses) and Settings.
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { BrowserStep, Check, CheckResult, checks, notExcluded, records, Step } from "../api";
import type { Ctx } from "../App";
import { Block, Card, Drawer, StatusStrip, Tabs } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";
import { TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtMs, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { fmtTs } from "./Logs";
import { describeConstraint, describeStep, every, isBrowser, OK, pct, ResultBox } from "./Synthetics";

const SUCCESS = "synthetics.check.success", DURATION = "synthetics.check.duration";
const TLS = "synthetics.check.tls_days_remaining", STEP_DURATION = "synthetics.step.duration";
export const LOCATION = "us-east-1 (N. Virginia)";
const RUNS = 200;

type Tab = "overview" | "runs" | "settings";
type Run = { id: string; ts: string; ok: boolean; excluded: string | null; ms: number | null; status: number | null;
             failure: string; failedStep: number | null; screenshots: number[] };

function toRun(r: Record<string, unknown>): Run {
  const a = (r.attributes as Record<string, unknown>) ?? {};
  return { id: String(r.trace_id), ts: String(r.ts), ok: a["check.result"] === "pass", excluded: (a["check.excluded"] as string) ?? null,
           ms: a["check.total_ms"] != null ? Number(a["check.total_ms"]) : r.duration_ns != null ? Number(r.duration_ns) / 1e6 : null,
           status: a["http.response.status_code"] != null ? Number(a["http.response.status_code"]) : null,
           failure: String(a["check.failure"] ?? ""), failedStep: a["check.failed_step"] != null ? Number(a["check.failed_step"]) : null,
           screenshots: String(a["check.screenshots"] ?? "").split(",").filter(Boolean).map(Number) };
}

const ago = (iso: string) => {
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
};
/** A URL with the check's (non-secret) variables filled in, for display. */
const withVars = (url: string, vars: Record<string, string>) => url.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (m, k) => vars[k] ?? m);
const span = (ms: number) => {
  const m = ms / 60000;
  return m < 90 ? `${Math.max(1, Math.round(m))} min` : m < 2880 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} days`;
};

export function CheckDetail({ ctx, id }: { ctx: Ctx; id: string }) {
  const [check, setCheck] = useState<Check | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [running, setRunning] = useState<CheckResult | "running" | null>(null);
  const [nonce, setNonce] = useState(0);
  const [openRun, setOpenRun] = useState<Run | null>(null);
  const load = useCallback(() => checks.get(id).then(setCheck, (e: Error) => setError(e.message)), [id]);
  useEffect(() => { load(); }, [load, ctx.tick]);

  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick, nonce]);
  const exRuns = (check?.exclusions ?? []).map((e) => e.run_id);
  const key = id + ctx.range.key + ctx.tick + nonce + ":" + exRuns.join(",").length;
  const runs = useQuery({ signal: "traces", ...w, services: ["synthetics"], search: { limit: RUNS },
                          where: [{ field: "attributes.check.id", op: "=", value: id }, { field: "attributes.check.result", op: "exists" }] }, "r" + key);

  if (error) return <div className="state error">{error}</div>;
  if (!check) return <div className="skeleton" style={{ height: 240 }} />;
  const runList = runs.data ? records(runs.data).map(toRun).sort((a, b) => b.ts.localeCompare(a.ts)) : [];
  const byRun = new Map((check.exclusions ?? []).map((e) => [e.run_id, e]));
  const act = async (f: () => Promise<unknown>) => { try { await f(); } catch (e) { setError((e as Error).message); } };
  const runNow = async () => {
    setRunning("running");
    try { setRunning((await checks.run(id)).result); setTimeout(() => setNonce((n) => n + 1), 60_000); }   // searchable within ~a minute
    catch (e) { setError((e as Error).message); setRunning(null); }
  };
  const last = runList[0];
  const state = !check.enabled ? ["Paused", ""] : !last ? ["No runs yet", ""] : last.ok ? ["Up", "ok"] : ["Down", "bad"];

  return (
    <>
      <div className="toolbar">
        <a href="#/synthetics" className="btn" onClick={(e) => { e.preventDefault(); ctx.go("/synthetics"); }}>← All checks</a>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn" disabled={running === "running"} onClick={runNow}>{running === "running" ? "Running…" : "Run now"}</button>
        <button className="btn" onClick={() => act(async () => setCheck(await checks.update(id, { enabled: !check.enabled })))}>{check.enabled ? "Pause" : "Resume"}</button>
        <button className="btn" onClick={() => ctx.go(`/alerts/rules/new?check=${id}`)}>Alert me</button>
        <button className="btn" onClick={() => ctx.go(`/synthetics/${id}/edit`)}>Edit</button>
        <button className="btn" onClick={() => confirm(`Delete the check “${check.name}”? Its past results stay in your data.`) &&
                                               act(async () => { await checks.remove(id); ctx.go("/synthetics"); })}>Delete</button>
      </div>
      <div className="page-head">
        <h1>{check.name}</h1>
        <span className={`pill ${state[1]}`}>{state[0]}</span>
        <span className="faint">{isBrowser(check) ? `Browser check (${check.device ?? "desktop"})` : "HTTP check"} · every {every(check.frequency)} from {LOCATION}</span>
      </div>
      {running && running !== "running" && <ResultBox result={running} note="Recorded; it appears in the charts and runs within about a minute." />}
      <section className="panel">
        <Tabs tabs={[["overview", "Overview"], ["runs", `Check runs${runs.data ? ` (${runList.length >= RUNS ? `${RUNS}+` : runList.length})` : ""}`], ["settings", "Settings"]]}
              active={tab} onPick={setTab} />
      </section>
      {tab === "overview" && <Overview ctx={ctx} check={check} runs={runs} runList={runList} exRuns={exRuns} w={w} rkey={key} onRun={setOpenRun} />}
      {tab === "runs" && <Runs ctx={ctx} check={check} runs={runs} runList={runList} byRun={byRun} onRun={setOpenRun}
                               onExclude={(run, reason) => act(async () => { await checks.exclude(id, run, reason || "Excluded"); await load(); })}
                               onInclude={(run) => act(async () => { await checks.include(id, run); await load(); })} />}
      {tab === "settings" && <Settings ctx={ctx} check={check} />}
      {openRun && <RunPanel ctx={ctx} check={check} run={openRun} excludedBy={byRun.get(openRun.id)} onClose={() => setOpenRun(null)}
                            onExclude={(reason) => act(async () => { await checks.exclude(id, openRun.id, reason || "Excluded"); await load(); })}
                            onInclude={() => act(async () => { await checks.include(id, openRun.id); await load(); })} />}
    </>
  );
}

// ------------------------------------------------------------------ overview

function Overview(p: { ctx: Ctx; check: Check; runs: ReturnType<typeof useQuery>; runList: Run[]; exRuns: string[];
                       w: { start: string; end: string }; rkey: string; onRun: (r: Run) => void }) {
  const { check, ctx } = p, id = check.id;
  const b = bucketSeconds(ctx.range);
  const mine = (metric: string) => [{ field: "metric_name", op: "=", value: metric }, { field: "attributes.check.id", op: "=", value: id }, ...notExcluded(p.exRuns)];
  const base = { signal: "metrics" as const, services: ["synthetics"] };
  // Everything follows the page's time range; uptime is also compared with the period just before it.
  const prevW = useMemo(() => {
    const a = Date.parse(p.w.start), z = Date.parse(p.w.end);
    return { start: new Date(a - (z - a)).toISOString(), end: new Date(a).toISOString() };
  }, [p.w.start, p.w.end]);
  const prev = useQuery({ ...base, ...prevW, where: mine(SUCCESS), aggs: [{ fn: "count" }, { fn: "sum", field: "value" }] }, "p" + p.rkey);
  const everyRun = useQuery({ ...base, ...p.w, where: mine(SUCCESS).slice(0, 2), aggs: [{ fn: "count" }] }, "a" + p.rkey);
  const speed = useQuery({ ...base, ...p.w, where: mine(DURATION), aggs: [{ fn: "avg", field: "value" }, { fn: "p95", field: "value" }] }, "s" + p.rkey);
  // Every run in the range (the run list stops at the latest RUNS)
  const tally = useQuery({ ...base, ...p.w, where: mine(SUCCESS), aggs: [{ fn: "count" }, { fn: "sum", field: "value" }] }, "f" + p.rkey);
  const tls = useQuery({ ...base, ...p.w, where: mine(TLS), aggs: [{ fn: "min", field: "value" }] }, "c" + p.rkey);
  const series = useQuery({ ...base, ...p.w, where: mine(DURATION), group_by: [`ts:${b}`], aggs: [{ fn: "avg", field: "value" }, { fn: "p95", field: "value" }], limit: 10000 }, "d" + p.rkey);
  const perStep = useQuery({ ...base, ...p.w, where: mine(STEP_DURATION), group_by: ["attributes.step.index"], aggs: [{ fn: "avg", field: "value" }, { fn: "p95", field: "value" }], limit: 20 }, "q" + p.rkey);

  const counted = p.runList.filter((r) => !r.excluded && !p.exRuns.includes(r.id));
  const t = tally.data?.rows[0], total = t ? Number(t[0]) : 0, fails = t ? Math.round(total - Number(t[1] ?? 0)) : 0;
  const lastFail = counted.find((r) => !r.ok), last = p.runList[0];
  const upFor = !last ? "—" : !last.ok ? "down" : lastFail ? span(Date.now() - Date.parse(lastFail.ts)) : `> ${span(Date.now() - Date.parse(p.w.start))}`;
  const share = (q: typeof tally) => { const r = q.data?.rows[0]; return r && Number(r[0]) ? Number(r[1] ?? 0) / Number(r[0]) : null; };
  const uptime = share(tally), before = share(prev);
  const excludedRuns = everyRun.data && t ? Math.max(0, Number(everyRun.data.rows[0]?.[0] ?? 0) - total) : null;
  const period = ctx.range.label.toLowerCase();
  const s = speed.data?.rows[0];
  const stepTimes = new Map((perStep.data ? records(perStep.data) : []).map((r) => [Number(r["attributes.step.index"]), r]));
  const first = check.steps[0] as Step | BrowserStep | undefined;
  const constraints = (check.steps as (Step | BrowserStep)[]).flatMap((st) => ("constraints" in st ? st.constraints : []));
  const pts = (i: number) => (series.data ? series.data.rows.map((r) => [Date.parse(String(r[0])), Number(r[i])] as [number, number]).filter((x) => isFinite(x[1])) : []);
  const strip = [...p.runList].reverse().slice(-120);

  return (
    <>
      <div className="grid">
        <Panel title="Configuration" span={5}>
          <div className="config">
            <span className="k">Target</span><span className="mono" style={{ wordBreak: "break-all" }}>{first ? ("url" in first && first.url ? withVars(first.url, check.variables) : describeStep(first)) : "—"}</span>
            <span className="k">Steps</span><span>{check.steps.length}{check.steps.length > 1 ? ` (${(check.steps as (Step | BrowserStep)[]).map((st) => st.name).join(" → ")})` : ""}</span>
            <span className="k">Expected</span>
            <span>{isBrowser(check) ? `every step succeeds${(check.steps as BrowserStep[]).some((st) => st.action.startsWith("assert")) ? " and every assertion holds" : ""}`
                   : constraints.length ? constraints.slice(0, 4).map(describeConstraint).join(" · ") + (constraints.length > 4 ? ` · +${constraints.length - 4} more` : "") : "status < 400"}</span>
            <span className="k">Retries</span><span>None: every run counts (exclude false alarms from the Check runs tab)</span>
            <span className="k">Schedule</span><span>Every {every(check.frequency)} from {LOCATION}{check.enabled ? "" : " · paused"}</span>
            <span className="k">Timeout</span><span>{fmtNum(check.timeout_ms / 1000)} s for the whole run</span>
          </div>
        </Panel>
        <div className="span-7">
          <div className="cards" style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
            <Card label="Uptime" value={uptime == null ? (t ? "—" : "…") : pct(uptime)} tone={uptime == null ? undefined : uptime >= 0.99 ? "ok" : "bad"}
                  sub={`${period}${before != null ? ` · ${pct(before)} the ${period.replace(/^last /, "")} before` : ""}`} />
            <Card label="Excluded runs" value={excludedRuns == null ? "…" : fmtNum(excludedRuns)}
                  sub="false alarms and maintenance windows; not counted" />
            <Card label="Average duration" value={s && s[0] != null ? `${fmtNum(Number(s[0]))} ms` : "—"} sub={s && s[1] != null ? `p95 ${fmtNum(Number(s[1]))} ms` : undefined} />
            <Card label="Failed runs" value={t ? fmtNum(fails) : "…"} tone={fails ? "bad" : undefined} sub={`of ${fmtNum(total)} in ${ctx.range.label.toLowerCase()}`} />
            <Card label="Last check" value={last ? ago(last.ts) : "—"} tone={last ? (last.ok ? "ok" : "bad") : undefined} sub={last ? (last.ok ? "passed" : "failed") : undefined}
                  onClick={last ? () => p.onRun(last) : undefined} />
            {isBrowser(check) || tls.data?.rows[0]?.[0] == null
              ? <Card label="Currently up for" value={upFor} tone={last && !last.ok ? "bad" : undefined} />
              : <Card label="Currently up for" value={upFor} tone={last && !last.ok ? "bad" : undefined}
                      sub={`certificate valid ${Math.floor(Number(tls.data!.rows[0][0]))} more days`} />}
          </div>
        </div>
      </div>
      <Panel title="Runs" right={<span className="faint">oldest → newest · click one for its details</span>}>
        <Loads q={p.runs} empty={!strip.length} height={40}>
          {() => <StatusStrip blocks={strip.map((r): Block => ({ key: r.id, ok: r.ok, excluded: !!r.excluded || p.exRuns.includes(r.id),
                                                                 title: `${fmtTs(r.ts).slice(0, 19)} · ${r.ok ? "passed" : "failed"}${r.ms != null ? ` · ${fmtNum(r.ms)} ms` : ""}` }))}
                                  height={30} onBlock={(i) => p.onRun(strip[i])} />}
        </Loads>
      </Panel>
      <div className="grid">
        <Panel title="Duration" span={8}>
          <Loads q={series} empty={!series.data?.rows.length} height={200}>
            {() => <TimeSeries area={false} series={[{ label: "average", color: "var(--series-1)", points: pts(1) }, { label: "p95", color: "var(--series-3)", points: pts(2) }]}
                               range={ctx.range} unit=" ms" height={200} />}
          </Loads>
        </Panel>
        <Panel title="Time per step" span={4} flush>
          <RankTable head={["#", "step", "avg", "p95"]} numCols={2}
                     rows={(check.steps as (Step | BrowserStep)[]).map((st, i) => {
                       const r = stepTimes.get(i + 1);
                       return [String(i + 1), st.name, r ? `${fmtNum(Number(r["avg(value)"]))} ms` : "—", r ? `${fmtNum(Number(r["p95(value)"]))} ms` : "—"];
                     })} />
        </Panel>
      </div>
    </>
  );
}

// ------------------------------------------------------------------ check runs

function Runs(p: { ctx: Ctx; check: Check; runs: ReturnType<typeof useQuery>; runList: Run[]; byRun: Map<string, { reason: string; by?: string }>;
                   onRun: (r: Run) => void; onExclude: (run: string, reason: string) => void; onInclude: (run: string) => void }) {
  const [result, setResult] = useState<"all" | "pass" | "fail">("all");
  const [showExcluded, setShowExcluded] = useState(true);
  const excludedOf = (r: Run) => r.excluded ?? (p.byRun.get(r.id) ? `excluded: ${p.byRun.get(r.id)!.reason}` : null);
  const shown = p.runList.filter((r) => (result === "all" || (result === "pass") === r.ok) && (showExcluded || !excludedOf(r)));
  const strip = [...p.runList].reverse().slice(-120);
  return (
    <>
      <Panel title="Runs" right={<span className="faint">oldest → newest</span>}>
        <Loads q={p.runs} empty={!strip.length} height={40}>
          {() => <StatusStrip blocks={strip.map((r) => ({ key: r.id, ok: r.ok, excluded: !!excludedOf(r), title: `${fmtTs(r.ts).slice(0, 19)} · ${r.ok ? "passed" : "failed"}` }))}
                              height={30} onBlock={(i) => p.onRun(strip[i])} />}
        </Loads>
      </Panel>
      <div className="toolbar">
        <div className="chips" role="radiogroup" aria-label="Result">
          {([["all", "All runs"], ["pass", "Passed"], ["fail", "Failed"]] as const).map(([k, l]) => (
            <button key={k} type="button" className={`chip${result === k ? " on" : ""}`} role="radio" aria-checked={result === k} onClick={() => setResult(k)}>{l}</button>
          ))}
        </div>
        <label className="faint" style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={showExcluded} onChange={(e) => setShowExcluded(e.target.checked)} />show excluded runs
        </label>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="faint">Exclude a false alarm (e.g. a deployment) so it doesn't count toward uptime and SLOs.</span>
      </div>
      <Panel title="Check runs" flush right={<span className="faint">{shown.length} shown · click one for its request and response</span>}>
        <Loads q={p.runs} empty={!shown.length} height={160}>
          {() => (
            <div className="table-scroll" style={{ maxHeight: 640 }}>
              <table className="dtable">
                <colgroup><col style={{ width: 190 }} /><col style={{ width: 90 }} /><col style={{ width: 130 }} /><col style={{ width: 80 }} />
                  <col style={{ width: 100 }} /><col style={{ width: 110 }} /><col /><col style={{ width: 100 }} /></colgroup>
                <thead><tr><th>Start time</th><th>Result</th><th>Location</th><th className="num">Retries</th><th className="num">Duration</th><th className="num">Status code</th><th>Violated assertions</th><th /></tr></thead>
                <tbody>
                  {shown.map((r) => {
                    const ex = excludedOf(r), manual = p.byRun.get(r.id);
                    return (
                      <tr key={r.id} onClick={() => p.onRun(r)} className={ex ? "excluded" : undefined}>
                        <td className="faint">{fmtTs(r.ts)}</td>
                        <td><span className={`pill ${r.ok ? "ok" : "bad"}`}>{r.ok ? "passed" : "failed"}</span></td>
                        <td className="muted">us-east-1</td>
                        <td className="num">0</td>
                        <td className="num">{r.ms != null ? fmtMs(r.ms * 1e6) : "—"}</td>
                        <td className="num">{r.status ?? "—"}</td>
                        <td title={r.failure || ex || undefined}>{r.failure ? r.failure : ex ? <span className="faint">{ex}</span> : <span className="faint">none</span>}</td>
                        <td onClick={(e) => e.stopPropagation()}>
                          {r.excluded ? <span className="faint" title={r.excluded}>maintenance</span>
                            : manual ? <button type="button" className="btn small" title={`Excluded by ${manual.by ?? "?"}: ${manual.reason}`} onClick={() => p.onInclude(r.id)}>Include</button>
                            : <button type="button" className="btn small" onClick={() => { const why = prompt("Why exclude this run? (e.g. Deployment)", "Deployment"); if (why != null) p.onExclude(r.id, why); }}>Exclude</button>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Loads>
      </Panel>
    </>
  );
}

// ------------------------------------------------------------------ one run

type StepSpan = { index: number; name: string; ok: boolean; failure: string; url: string; status: number | null; ms: number; start: number;
                  timings: [string, number][]; action?: string; screenshot: boolean;
                  method?: string; reqHeaders?: Record<string, string>; reqBody?: string; respHeaders?: Record<string, string>; respBody?: string };

const parse = (v: unknown): Record<string, string> | undefined => {
  if (v == null) return undefined;
  try { return JSON.parse(String(v)); } catch { return undefined; }
};

function RunPanel(p: { ctx: Ctx; check: Check; run: Run; excludedBy?: { reason: string; by?: string }; onClose: () => void;
                       onExclude: (reason: string) => void; onInclude: () => void }) {
  const browser = isBrowser(p.check);
  const [tab, setTab] = useState<"overview" | "request" | "response" | "screenshots">("overview");
  const [sel, setSel] = useState<number | null>(null);
  const t = Date.parse(p.run.ts);
  const w = { start: new Date(t - 3600_000).toISOString(), end: new Date(t + 3600_000).toISOString() };
  const spans = useQuery({ signal: "traces", ...w, services: ["synthetics"], match: { trace_id: p.run.id }, search: { limit: 100 } }, "run" + p.run.id);
  const steps: StepSpan[] = useMemo(() => (spans.data ? records(spans.data) : [])
    .filter((r) => (r.attributes as Record<string, unknown>)?.["step.index"] != null)
    .map((r) => {
      const a = r.attributes as Record<string, unknown>;
      return { index: Number(a["step.index"]), name: String(a["step.name"] ?? r.name), ok: a["step.result"] !== "fail", failure: String(a["step.failure"] ?? ""),
               url: String(a["url.full"] ?? ""), status: a["http.response.status_code"] != null ? Number(a["http.response.status_code"]) : null,
               ms: Number(r.duration_ns ?? 0) / 1e6, start: Number(r.ts_unix_nano ?? Date.parse(String(r.ts)) * 1e6),
               timings: (["dns_ms", "connect_ms", "tls_ms", "ttfb_ms", "total_ms", "fcp_ms", "lcp_ms", "load_ms"] as const)
                 .filter((k) => a[`step.${k}`] != null).map((k): [string, number] => [k.replace("_ms", "").replace("ttfb", "first byte").replace("fcp", "first paint").replace("lcp", "largest paint"), Number(a[`step.${k}`])]),
               action: a["step.action"] as string | undefined, screenshot: !!a["step.screenshot"],
               method: a["http.request.method"] as string | undefined, reqHeaders: parse(a["http.request.headers"]), reqBody: a["http.request.body"] as string | undefined,
               respHeaders: parse(a["http.response.headers"]), respBody: a["http.response.body"] as string | undefined };
    }).sort((a, b) => a.index - b.index), [spans.data]);
  const cur = steps.find((s) => s.index === sel) ?? steps.find((s) => !s.ok) ?? steps[steps.length - 1];
  const excluded = p.run.excluded ?? (p.excludedBy ? `Excluded: ${p.excludedBy.reason}${p.excludedBy.by ? ` (by ${p.excludedBy.by})` : ""}` : null);
  const tabs: [typeof tab, string][] = browser ? [["overview", "Overview"], ["screenshots", "Screenshots"]] : [["overview", "Overview"], ["request", "Request"], ["response", "Response"]];
  const t0 = steps.length ? Math.min(...steps.map((s) => s.start)) : 0;
  const total = steps.length ? Math.max(...steps.map((s) => (s.start - t0) / 1e6 + s.ms), 1) : 1;

  return (
    <Drawer onClose={p.onClose} title={<><span className={`pill ${p.run.ok ? "ok" : "bad"}`} style={{ marginRight: 8 }}>{p.run.ok ? "passed" : "failed"}</span>{p.check.name} · {fmtTs(p.run.ts).slice(0, 19)}</>}
            right={<>
              {!p.run.ok && <button type="button" className="btn ask-btn" onClick={() => p.ctx.go(`/ai?ask=${encodeURIComponent(`Why did the "${p.check.name}" check fail at ${p.run.ts}? Its run is trace \`${p.run.id}\`. Is the problem on our side?`)}&page=${encodeURIComponent(JSON.stringify({ check: p.check.id, run: p.run.id }))}`)}>Why did it fail?</button>}
              <button type="button" className="btn" onClick={() => p.ctx.go(`/traces/${p.run.id}`)}>Open trace</button></>}>
      <Tabs tabs={tabs} active={tab} onPick={setTab} />
      {tab === "overview" && (
        <>
          <div className="drawer-section">
            <div className="config">
              <span className="k">Result</span><span style={{ color: p.run.ok ? OK : "var(--sev-error)" }}>{p.run.ok ? "Passed" : `Failed: ${p.run.failure}`}</span>
              <span className="k">Started</span><span className="mono">{fmtTs(p.run.ts)}</span>
              <span className="k">Duration</span><span className="mono">{p.run.ms != null ? fmtMs(p.run.ms * 1e6) : "—"}</span>
              <span className="k">Location</span><span>{LOCATION}</span>
              <span className="k">Retries</span><span>0</span>
              <span className="k">Run ID</span><span className="mono faint">{p.run.id}</span>
              <span className="k">Counts toward uptime</span>
              <span>{excluded ? <>{`No — ${excluded}`} {p.excludedBy && <button type="button" className="btn small" onClick={p.onInclude}>Include again</button>}</>
                : <>Yes {!p.run.excluded && <button type="button" className="btn small" onClick={() => { const why = prompt("Why exclude this run? (e.g. Deployment)", "Deployment"); if (why != null) p.onExclude(why); }}>Exclude</button>}</>}</span>
            </div>
          </div>
          <div className="drawer-section">
            <h4>Steps</h4>
            <Loads q={spans} empty={!steps.length} height={80}>
              {() => (
                <div>
                  {steps.map((s) => (
                    <div key={s.index} className="wf-row" onClick={() => { setSel(s.index); if (!browser) setTab("response"); }} title={browser ? undefined : "Show its request and response"}>
                      <div className="wf-name">
                        <span style={{ color: s.ok ? OK : "var(--sev-error)" }}>{s.ok ? "✓" : "✕"}</span> {s.index}. {s.name}
                        {s.status != null && <span className="faint"> · {s.status}</span>}
                      </div>
                      <div className="wf-track">
                        <div className="wf-bar" style={{ left: `${(((s.start - t0) / 1e6) / total) * 100}%`, width: `${Math.max(0.5, (s.ms / total) * 100)}%`, background: s.ok ? "var(--accent)" : "var(--sev-error)" }} />
                        <span className="wf-dur" style={{ right: 0 }}>{fmtMs(s.ms * 1e6)}</span>
                      </div>
                    </div>
                  ))}
                  {steps.filter((s) => s.failure).map((s) => <div key={s.index} className="form-error" style={{ marginTop: 8 }}>Step {s.index}: {s.failure}</div>)}
                  {cur && cur.timings.length > 0 && <div className="faint mono" style={{ marginTop: 8 }}>{cur.index}. {cur.name}: {cur.timings.map(([k, v]) => `${k} ${fmtNum(v)} ms`).join(" · ")}</div>}
                </div>
              )}
            </Loads>
          </div>
        </>
      )}
      {(tab === "request" || tab === "response") && (
        <Loads q={spans} empty={!steps.length} height={80}>
          {() => (
            <>
              {steps.length > 1 && (
                <div className="drawer-section chips">
                  {steps.map((s) => (
                    <button key={s.index} type="button" className={`chip${cur?.index === s.index ? " on" : ""}`} onClick={() => setSel(s.index)}>
                      <span style={{ color: s.ok ? OK : "var(--sev-error)" }}>{s.ok ? "✓" : "✕"}</span> {s.index}. {s.name}
                    </button>
                  ))}
                </div>
              )}
              {cur && tab === "request" && (
                <>
                  <div className="drawer-section"><h4>Request</h4><div className="mono" style={{ wordBreak: "break-all" }}><b>{cur.method ?? "GET"}</b> {cur.url}</div></div>
                  <div className="drawer-section"><h4>Headers</h4><Headers h={cur.reqHeaders} /></div>
                  <div className="drawer-section"><h4>Body</h4>{cur.reqBody ? <pre className="pre">{pretty(cur.reqBody)}</pre> : <div className="faint">No body</div>}</div>
                </>
              )}
              {cur && tab === "response" && (
                <>
                  <div className="drawer-section"><h4>Response</h4>
                    <div className="mono">{cur.status != null ? <b style={{ color: cur.ok ? OK : "var(--sev-error)" }}>HTTP {cur.status}</b> : <span className="faint">No response</span>} <span className="faint">· {fmtMs(cur.ms * 1e6)}</span></div>
                    {cur.failure && <div className="form-error" style={{ marginTop: 6 }}>{cur.failure}</div>}
                  </div>
                  <div className="drawer-section"><h4>Headers</h4><Headers h={cur.respHeaders} /></div>
                  <div className="drawer-section"><h4>Body</h4>{cur.respBody ? <pre className="pre">{pretty(cur.respBody)}</pre>
                    : <div className="faint">Not recorded (the step's “record the response body” is off, or the response had none).</div>}
                    {cur.respBody && cur.respBody.length >= 2048 && <div className="faint" style={{ marginTop: 4 }}>The first 2 KB.</div>}</div>
                </>
              )}
              <div className="drawer-section faint">Passwords, tokens, cookies and your secrets are hidden (••••) before anything is recorded.</div>
            </>
          )}
        </Loads>
      )}
      {tab === "screenshots" && <Screenshots check={p.check} run={p.run} />}
    </Drawer>
  );
}

function Headers({ h }: { h?: Record<string, string> }) {
  if (!h || !Object.keys(h).length) return <div className="faint">Not recorded for this run.</div>;
  return (
    <div className="kv mono" style={{ fontSize: 12 }}>
      {Object.entries(h).map(([k, v]) => <Fragment key={k}><span className="k">{k}</span><span className="v">{v}</span></Fragment>)}
    </div>
  );
}

const pretty = (s: string) => { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; } };

function Screenshots({ check, run }: { check: Check; run: Run }) {
  const [imgs, setImgs] = useState<Record<number, string | { error: string }>>({});
  useEffect(() => {
    let live = true;
    for (const n of run.screenshots)
      checks.screenshot(check.id, run.id, n).then((r) => live && setImgs((m) => ({ ...m, [n]: r.image })),
                                                  (e: Error) => live && setImgs((m) => ({ ...m, [n]: { error: e.message } })));
    return () => { live = false; };
  }, [check.id, run.id]);   // eslint-disable-line react-hooks/exhaustive-deps
  if (!run.screenshots.length) return <div className="state">No screenshots for this run. They are taken when a step fails, or after every step if the check is set to.</div>;
  return (
    <>
      {run.screenshots.map((n) => {
        const img = imgs[n];
        return (
          <div key={n} className="drawer-section">
            <h4>After step {n}: {check.steps[n - 1]?.name ?? ""}</h4>
            {!img ? <div className="skeleton" style={{ height: 260 }} /> : typeof img !== "string" ? <div className="state error">{img.error}</div>
              : <img className="shot" src={`data:image/jpeg;base64,${img}`} alt={`Screenshot after step ${n}`} />}
          </div>
        );
      })}
    </>
  );
}

// ------------------------------------------------------------------ settings

function Settings({ ctx, check }: { ctx: Ctx; check: Check }) {
  const steps = check.steps as (Step | BrowserStep)[];
  return (
    <>
      <Panel title="Settings" right={<button className="btn" onClick={() => ctx.go(`/synthetics/${check.id}/edit`)}>Edit</button>}>
        <div className="config">
          <span className="k">Name</span><span>{check.name}</span>
          <span className="k">Type</span><span>{isBrowser(check) ? `Browser (${check.device ?? "desktop"}; screenshots ${check.screenshots === "every_step" ? "after every step" : "when a step fails"})` : "HTTP"}</span>
          <span className="k">Frequency</span><span>Every {every(check.frequency)}</span>
          <span className="k">Location</span><span>{LOCATION}</span>
          <span className="k">Timeout</span><span>{fmtNum(check.timeout_ms / 1000)} s</span>
          <span className="k">State</span><span>{check.enabled ? "Running" : "Paused"}</span>
          <span className="k">Variables</span><span className="mono">{Object.keys(check.variables).length ? Object.entries(check.variables).map(([k, v]) => `{${k}} = ${v}`).join(" · ") : "—"}</span>
          <span className="k">Secrets</span><span className="mono">{check.secret_names.length ? check.secret_names.map((s) => `{${s}}`).join(" ") + " (values are never shown)" : "—"}</span>
          <span className="k">Created</span><span>{fmtTs(check.created_at).slice(0, 16)}{check.created_by ? ` by ${check.created_by}` : ""}</span>
          <span className="k">Changed</span><span>{fmtTs(check.updated_at).slice(0, 16)}</span>
        </div>
      </Panel>
      <Panel title="Steps" flush>
        <RankTable head={["#", "step", isBrowser(check) ? "action" : "request", "assertions"]} numCols={0}
                   rows={steps.map((st, i) => [String(i + 1), st.name, describeStep(st),
                                               "constraints" in st ? [st.auth.type !== "none" ? `${st.auth.type} auth` : "", ...st.constraints.map(describeConstraint),
                                                                      ...st.extract.map((e) => `→ {${e.name}}`)].filter(Boolean).join(" · ") : ""])} />
      </Panel>
    </>
  );
}
