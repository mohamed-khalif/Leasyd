// Reliability & cost report (lib/report.ts): AI agents, services, uptime and data/cost over the page's
// time range, with the top 3 things to fix. Share link: a snapshot anyone with the link can open for
// 30 days (SharedReport, no sign-in); Download PDF: the browser's print to PDF of this page.
import { useMemo, useState } from "react";
import { fmtCost, fmtDateTime, fmtDay, fmtMs, fmtNum, fmtPercent } from "../lib/format";
import { account, notExcluded, records, Result, sharedReport, shareReport } from "../api";
import type { Ctx } from "../App";
import { isDemo } from "../auth";
import { useCheckList } from "../checkList";
import { Loads, Panel } from "../components/Panel";
import { errorsQuery, emptyQuery, findIssues, loopsQuery, mergeSessions, sessionsQuery } from "../lib/agents";
import { buildReport, Report as ReportData } from "../lib/report";
import { ENTRY, useServices } from "../lib/services";
import { useAccount } from "../useAccount";
import { useLoad, useQuery, useSql } from "../useQuery";
import { useRangeWindow } from "../useNow";

const rows = (r: Result | null) => (r ? records(r) : []);

/** Print to PDF in the light theme (for paper and email), then back to the viewer's theme. */
function printLight() {
  const el = document.documentElement, was = el.dataset.theme;
  el.dataset.theme = "light";
  const restore = () => { if (was === undefined) delete el.dataset.theme; else el.dataset.theme = was; removeEventListener("afterprint", restore); };
  addEventListener("afterprint", restore);
  window.print();
}
const SUCCESS = "synthetics.check.success";

export function Report({ ctx }: { ctx: Ctx }) {
  const w = useRangeWindow(ctx);
  const key = ctx.range.key + ctx.tick;
  const { account: acc } = useAccount(ctx.tick);
  const svcs = useServices(ctx);
  const errMsgs = useSql(`SELECT service, status_message AS message, count(*) AS n FROM spans
    WHERE status_code = 2 AND ${ENTRY} AND status_message IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 200`, w, "m" + key);
  const list = useQuery(sessionsQuery(w), "s" + key), errors = useQuery(errorsQuery(w), "e" + key);
  const empty = useQuery(emptyQuery(w), "y" + key), loops = useQuery(loopsQuery(w), "l" + key);
  const checkList = useCheckList(ctx.tick);
  const excluded = (checkList.checks ?? []).flatMap((c) => c.excluded_runs ?? []);
  const uptime = useQuery(checkList.checks?.length ? { signal: "metrics", ...w, services: ["synthetics"], group_by: ["attributes.check.id"],
    where: [{ field: "metric_name", op: "=", value: SUCCESS }, ...notExcluded(excluded)], aggs: [{ fn: "avg", field: "value" }, { fn: "count" }], limit: 1000 } : null,
    "u" + key + (checkList.checks?.length ?? "-") + ":" + excluded.length);
  const days = Math.min(31, Math.ceil(ctx.range.minutes / 1440) + 1);
  const meters = useLoad(() => account.meters(days), "d" + key + days);

  // Each section waits for its own data; one that fails is left out rather than holding up the rest.
  const done = (q: { data: unknown; error: string | null }) => q.data != null || q.error != null;
  const ready = !!acc && done(svcs.loaded) && done(errMsgs) && done(list) && done(errors) && done(empty) && done(loops)
    && (checkList.checks != null || checkList.error != null) && (!checkList.checks?.length || done(uptime)) && done(meters);
  const report = useMemo(() => {
    if (!ready) return null;
    const sessions = list.data ? mergeSessions(rows(list.data)) : null;
    const issues = sessions ? findIssues(sessions, rows(errors.data), rows(empty.data), rows(loops.data)) : [];
    const up = new Map(rows(uptime.data).map((r) => [String(r["attributes.check.id"]), r]));
    const startDay = w.start.slice(0, 10);
    return buildReport({
      company: acc!.company, now: new Date(), period: { ...w, label: ctx.range.label },
      sessions, issues, services: svcs.loaded.data ? svcs.nodes : null,
      errorMessages: rows(errMsgs.data).map((r) => ({ service: String(r.service), message: String(r.message ?? ""), n: Number(r.n) })),
      checks: checkList.checks ? checkList.checks.filter((c) => up.has(c.id)).map((c) => ({
        name: c.name, uptimePct: Number(up.get(c.id)!["avg(value)"]) * 100, runs: Number(up.get(c.id)!.count) })) : null,
      meters: meters.data ? meters.data.days.filter((d) => d.day >= startDay) : null,
    });
  }, [ready]);   // eslint-disable-line react-hooks/exhaustive-deps -- built once per load: all inputs are in place when ready flips

  const [link, setLink] = useState<{ url: string; until: string } | null>(null);
  const [sharing, setSharing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const share = async () => {
    if (!report) return;
    setSharing(true); setError(null);
    try {
      const r = await shareReport(report);
      const url = `${location.origin}${location.pathname}#/r/${r.id}`;
      setLink({ url, until: r.expires_at });
      await navigator.clipboard?.writeText(url).catch(() => undefined);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSharing(false);
    }
  };

  return (
    <div className="report">
      <div className="report-actions no-print">
        {ctx.range.minutes < 1440 && <span className="faint">Reports usually cover a week: <a href="#/report?range=7d">use the last 7 days</a>.</span>}
        <span className="spacer" />
        {!isDemo() && <button className="btn" disabled={!report || sharing} onClick={share}>{sharing ? "Sharing…" : "Share link"}</button>}
        <button className="btn primary" disabled={!report} onClick={printLight}>Download PDF</button>
      </div>
      {link && <div className="form-note no-print" role="status">Link copied: <a href={link.url}>{link.url}</a> · works until {fmtDay(Date.parse(link.until))}.
        Anyone with it sees this report and nothing else.</div>}
      {error && <div className="form-error no-print" role="alert">{error}</div>}
      <Loads q={{ data: report, error: null, loading: !report }} height={320}>
        {() => <ReportView report={report!} inApp />}
      </Loads>
    </div>
  );
}

/** #/r/<id>: a shared report, opened by anyone with the link (no sign-in). */
export function SharedReport({ id }: { id: string }) {
  const got = useLoad(() => sharedReport(id), id);
  return (
    <div className="report report-shared">
      <div className="report-actions no-print">
        <span className="faint">Shared from Leasyd{got.data ? ` · link works until ${fmtDay(Date.parse(got.data.expires_at))}` : ""}</span>
        <span className="spacer" />
        <button className="btn primary" disabled={!got.data} onClick={printLight}>Download PDF</button>
      </div>
      <Loads q={got} height={320}>{() => <ReportView report={got.data!.report} />}</Loads>
    </div>
  );
}

// ------------------------------------------------------------------ the report itself

function ReportView({ report: r, inApp }: { report: ReportData; inApp?: boolean }) {
  const a = r.agents, s = r.services, u = r.uptime, use = r.usage;
  return (
    <article className="report-doc">
      <header className="report-head">
        <div className="faint">Reliability &amp; cost report</div>
        <h1>{r.company}</h1>
        <div className="faint">{fmtDateTime(r.period.start)} – {fmtDateTime(r.period.end)} ({r.period.label.toLowerCase()})</div>
      </header>

      <Panel title="Top things to fix">
        {r.fixes.length ? <ol className="report-fixes">{r.fixes.map((f) => <li key={f}>{f}</li>)}</ol>
          : <p className="faint">Nothing stands out: no service above 1% errors, no agent issues, every check at 99.9% or more.</p>}
      </Panel>

      <div className="report-tiles">
        <Tile label="AI conversations" value={a ? fmtNum(a.conversations) : "–"} sub={a ? `${fmtPercent((a.failed / a.conversations) * 100)} failed` : "no agent data"} bad={!!a && a.failed > 0} />
        <Tile label="AI cost" value={a?.cost != null ? fmtCost(a.cost) : "–"} sub={a?.costEstimated ? "estimated from list prices" : "as reported"} />
        <Tile label="Requests" value={s ? fmtNum(s.requests) : "–"} sub={s ? `${fmtPercent((s.errors / Math.max(s.requests, 1)) * 100)} errors · ${s.count} services` : "no service data"} bad={!!s && s.errors / Math.max(s.requests, 1) >= 0.01} />
        <Tile label="Leasyd cost" value={use ? fmtCost(use.cost) : "–"} sub={use ? `${fmtNum(use.records)} records${use.dropped ? `, ${fmtNum(use.dropped)} dropped` : ""}` : ""} />
      </div>

      {a && (
        <Panel title="AI agents" flush>
          <table className="dtable">
            <thead><tr><th style={{ width: "60%" }}>Issue</th><th className="num">Conversations</th></tr></thead>
            <tbody>{a.issues.length ? a.issues.map((i) => <tr key={i.title}><td>{i.title}</td><td className="num">{fmtNum(i.conversations)}</td></tr>)
              : <tr><td colSpan={2} className="faint">No silent failures found</td></tr>}</tbody>
          </table>
          {a.expensive.length > 0 && (
            <table className="dtable">
              <thead><tr><th style={{ width: "40%" }}>Most expensive conversations</th><th>Agent</th><th>Started</th><th className="num">LLM calls</th><th className="num">Cost</th></tr></thead>
              <tbody>{a.expensive.map((c) => (
                <tr key={c.id}>
                  <td className="mono" title={c.id}>{inApp ? <a href={`#/agents?session=${encodeURIComponent(c.id)}`}>{c.id}</a> : c.id}</td>
                  <td>{c.agent}</td><td>{fmtDateTime(c.start)}</td><td className="num">{fmtNum(c.llmCalls)}</td><td className="num">{fmtCost(c.cost)}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </Panel>
      )}

      {s && (
        <Panel title="Services with the most errors" flush>
          {s.worst.length ? (
            <table className="dtable">
              <thead><tr><th>Service</th><th className="num">Requests</th><th className="num">Error rate</th><th className="num">Duration avg</th><th style={{ width: "40%" }}>Most common error</th></tr></thead>
              <tbody>{s.worst.map((w) => (
                <tr key={w.service}>
                  <td>{inApp ? <a href={`#/apm/${encodeURIComponent(w.service)}`}>{w.service}</a> : w.service}</td>
                  <td className="num">{fmtNum(w.requests)}</td><td className={`num${w.errorPct >= 1 ? " bad" : ""}`}>{fmtPercent(w.errorPct)}</td>
                  <td className="num">{fmtMs(w.avgMs * 1e6)}</td><td title={w.topError}>{w.topError}</td>
                </tr>
              ))}</tbody>
            </table>
          ) : <p className="faint" style={{ padding: "0 14px" }}>No service had errors.</p>}
        </Panel>
      )}

      {u && (
        <Panel title="Uptime" flush>
          <table className="dtable">
            <thead><tr><th style={{ width: "60%" }}>Check</th><th className="num">Runs</th><th className="num">Uptime</th></tr></thead>
            <tbody>{u.checks.map((c) => (
              <tr key={c.name}><td>{c.name}</td><td className="num">{fmtNum(c.runs)}</td>
                <td className={`num${c.uptimePct < 99.9 ? " bad" : ""}`}>{fmtPercent(c.uptimePct)}</td></tr>
            ))}</tbody>
          </table>
        </Panel>
      )}
      <footer className="faint report-foot">Generated {fmtDateTime(r.generated_at)} by Leasyd. Costs at list prices.</footer>
    </article>
  );
}

function Tile({ label, value, sub, bad }: { label: string; value: string; sub: string; bad?: boolean }) {
  return (
    <div className="report-tile">
      <span className="faint">{label}</span>
      <b className={bad ? "bad" : undefined}>{value}</b>
      <span className="faint">{sub}</span>
    </div>
  );
}
