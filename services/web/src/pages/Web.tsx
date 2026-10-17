// Web monitoring (real users): what the browser script (examples/web/leasyd-rum.js, served at
// /install/leasyd-rum.js) sends through POST /v1/rum, stored as the tenant's logs with
// telemetry.sdk.name = leasyd-rum and rum.type = page_view | web_vital | js_error | api_call
// (services/ingest/rum.py). Per site (service): page views and visits, Core Web Vitals at the 75th
// percentile (as Google rates them) with the share of good ones, pages, JavaScript errors, failing
// or slow API calls (each linked to its backend trace) and browsers; a visit opens with its events.
import { useState } from "react";
import { records, Result } from "../api";
import type { Ctx } from "../App";
import { Card, Drawer, StackedBars } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { bucketSeconds, fmtNum, fmtTime } from "../time";
import { useSql } from "../useQuery";
import { useRangeWindow } from "../useNow";

const RUM = "resource_attributes['telemetry.sdk.name'] = 'leasyd-rum'";
const A = (k: string) => `attributes['${k}']`;
const N = (k: string) => `TRY_CAST(attributes['${k}'] AS DOUBLE)`;
const q = (s: string) => `'${s.replace(/'/g, "''")}'`;
const rows = (r: Result | null) => (r ? records(r) : []);
// Google's thresholds (web.dev): good up to the first, poor above the second
export const VITALS: [string, string, number, number, (v: number) => string][] = [
  ["LCP", "Largest Contentful Paint", 2500, 4000, (v) => `${(v / 1000).toFixed(2)} s`],
  ["INP", "Interaction to Next Paint", 200, 500, (v) => `${Math.round(v)} ms`],
  ["CLS", "Cumulative Layout Shift", 0.1, 0.25, (v) => v.toFixed(3)],
  ["FCP", "First Contentful Paint", 1800, 3000, (v) => `${(v / 1000).toFixed(2)} s`],
  ["TTFB", "Time to First Byte", 800, 1800, (v) => `${Math.round(v)} ms`],
];
export const rate = (name: string, v: number) => {
  const d = VITALS.find((x) => x[0] === name);
  return !d ? "" : v <= d[2] ? "good" : v <= d[3] ? "needs-improvement" : "poor";
};
const TONE = { good: "ok", "needs-improvement": "warn", poor: "bad", "": undefined } as const;

export function Web({ ctx }: { ctx: Ctx }) {
  const w = useRangeWindow(ctx.range, ctx.tick);
  const key = ctx.range.key + ctx.tick;
  const apps = useSql(`SELECT service, count(*) FILTER (WHERE ${A("rum.type")} = 'page_view') AS views
    FROM logs WHERE ${RUM} GROUP BY 1 ORDER BY 2 DESC`, w, "apps" + key);
  const list = rows(apps.data).map((r) => String(r.service));
  const [picked, setPicked] = useState<string | null>(null);
  const app = picked && list.includes(picked) ? picked : list[0];
  if (!apps.loading && !list.length) {
    return (
      <div className="web">
        <Panel title="Web monitoring">
          <div style={{ padding: 16 }}>
            <p>No real-user data in this time range yet.</p>
            <p className="muted">Add Leasyd's browser script to your site: <a href="#/start">Get started › Website (real users)</a>. It sends
              page loads, Core Web Vitals, JavaScript errors and slow or failed API calls from your visitors' browsers.</p>
          </div>
        </Panel>
      </div>
    );
  }
  return (
    <div className="web">
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        {list.length > 1 && (
          <select className="select" value={app ?? ""} onChange={(e) => setPicked(e.target.value)} aria-label="Site">
            {list.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        )}
        {list.length === 1 && <b>{app}</b>}
        <span className="faint">real users · {ctx.range.label.toLowerCase()}</span>
      </div>
      {app && <Site ctx={ctx} app={app} w={w} k={key + app} />}
    </div>
  );
}

function Site({ ctx, app, w, k }: { ctx: Ctx; app: string; w: { start: string; end: string }; k: string }) {
  const S = `${RUM} AND service = ${q(app)}`;
  const b = bucketSeconds(ctx.range);
  const [session, setSession] = useState<string | null>(null);
  const totals = useSql(`SELECT count(*) FILTER (WHERE ${A("rum.type")} = 'page_view') AS views,
      count(DISTINCT ${A("rum.session_id")}) AS visits,
      count(*) FILTER (WHERE ${A("rum.type")} = 'js_error') AS errors,
      count(*) FILTER (WHERE ${A("rum.type")} = 'api_call' AND (TRY_CAST(${A("http.response.status_code")} AS INT) >= 500
                                                             OR ${A("http.response.status_code")} = '0')) AS failed_calls,
      quantile_cont(${N("rum.load_ms")}, 0.75) FILTER (WHERE ${A("rum.type")} = 'page_view' AND ${A("rum.spa")} = 'false') AS load75
    FROM logs WHERE ${S}`, w, "tot" + k);
  const vitals = useSql(`SELECT ${A("rum.vital.name")} AS name, quantile_cont(${N("rum.vital.value")}, 0.75) AS p75, count(*) AS n,
      count(*) FILTER (WHERE ${A("rum.vital.rating")} = 'good') AS good
    FROM logs WHERE ${S} AND ${A("rum.type")} = 'web_vital' GROUP BY 1`, w, "vit" + k);
  const views = useSql(`SELECT time_bucket(INTERVAL '${b} seconds', ts) AS t,
      count(*) FILTER (WHERE ${A("rum.type")} = 'page_view') AS views, count(*) FILTER (WHERE ${A("rum.type")} = 'js_error') AS errors
    FROM logs WHERE ${S} GROUP BY 1 ORDER BY 1`, w, "views" + k);
  const pages = useSql(`SELECT ${A("url.path")} AS page, count(*) FILTER (WHERE ${A("rum.type")} = 'page_view') AS views,
      quantile_cont(${N("rum.load_ms")}, 0.75) FILTER (WHERE ${A("rum.type")} = 'page_view' AND ${A("rum.spa")} = 'false') AS load75,
      quantile_cont(${N("rum.vital.value")}, 0.75) FILTER (WHERE ${A("rum.vital.name")} = 'LCP') AS lcp75,
      quantile_cont(${N("rum.vital.value")}, 0.75) FILTER (WHERE ${A("rum.vital.name")} = 'INP') AS inp75,
      count(*) FILTER (WHERE ${A("rum.type")} = 'js_error') AS errors
    FROM logs WHERE ${S} GROUP BY 1 ORDER BY 2 DESC LIMIT 50`, w, "pages" + k);
  const errors = useSql(`SELECT ${A("error.message")} AS message, count(*) AS n, count(DISTINCT ${A("rum.session_id")}) AS visits,
      max(ts) AS last, any_value(${A("url.path")}) AS page, any_value(${A("error.source")}) AS source,
      any_value(${A("rum.session_id")}) AS session
    FROM logs WHERE ${S} AND ${A("rum.type")} = 'js_error' GROUP BY 1 ORDER BY 2 DESC LIMIT 20`, w, "err" + k);
  const calls = useSql(`SELECT ${A("http.request.method")} AS method, ${A("url.full")} AS url, ${A("http.response.status_code")} AS status,
      count(*) AS n, quantile_cont(${N("rum.duration_ms")}, 0.75) AS p75, max(trace_id) AS trace
    FROM logs WHERE ${S} AND ${A("rum.type")} = 'api_call' GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 20`, w, "calls" + k);
  const browsers = useSql(`SELECT resource_attributes['browser.name'] AS browser, resource_attributes['device.type'] AS device,
      count(DISTINCT ${A("rum.session_id")}) AS visits, count(*) FILTER (WHERE ${A("rum.type")} = 'js_error') AS errors
    FROM logs WHERE ${S} GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 12`, w, "br" + k);

  const t = rows(totals.data)[0] ?? {};
  const byVital = new Map(rows(vitals.data).map((r) => [String(r.name), r]));
  const bars = rows(views.data).map((r) => ({ t: Date.parse(String(r.t)), values: [Number(r.views ?? 0), Number(r.errors ?? 0)] }));

  return (
    <>
      <div className="cards" style={{ gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
        <Card label="Page views" value={fmtNum(Number(t.views ?? 0))} />
        <Card label="Visits" value={fmtNum(Number(t.visits ?? 0))} sub="sessions (30 min without activity ends one)" />
        <Card label="Page load (p75)" value={t.load75 == null ? "–" : `${(Number(t.load75) / 1000).toFixed(2)} s`} sub="full page loads" />
        <Card label="JavaScript errors" value={fmtNum(Number(t.errors ?? 0))} tone={Number(t.errors) ? "bad" : undefined} />
        <Card label="Failed API calls" value={fmtNum(Number(t.failed_calls ?? 0))} sub="5xx or no answer" tone={Number(t.failed_calls) ? "bad" : undefined} />
      </div>

      <Panel title="Core Web Vitals" right={<span className="faint">75th percentile of real visits, rated as Google does</span>}>
        <Loads q={vitals} empty={!byVital.size} height={90}>
          {() => (
            <div className="cards" style={{ padding: 12, gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
              {VITALS.map(([name, label, , , fmt]) => {
                const r = byVital.get(name);
                const v = r ? Number(r.p75) : null;
                const good = r && Number(r.n) ? Math.round((Number(r.good) / Number(r.n)) * 100) : null;
                return <Card key={name} label={`${name} · ${label}`} value={v == null ? "–" : fmt(v)}
                             tone={v == null ? undefined : TONE[rate(name, v) as keyof typeof TONE]}
                             sub={good == null ? "no data" : `${good}% good · ${fmtNum(Number(r!.n))} measured`} />;
              })}
            </div>
          )}
        </Loads>
      </Panel>

      <Panel title="Page views and JavaScript errors">
        <Loads q={views} empty={!bars.length} height={150}>
          {() => <StackedBars bars={bars} range={ctx.range} bucketMs={b * 1000}
                              keys={[{ label: "page views", color: "var(--accent)" }, { label: "JavaScript errors", color: "var(--sev-error)" }]} />}
        </Loads>
      </Panel>

      <Panel title="Pages" flush>
        <Loads q={pages} empty={!rows(pages.data).length} height={120}>
          {() => (
            <table className="dtable">
              <thead><tr><th style={{ width: "40%" }}>Page</th><th className="num">Views</th><th className="num">Load p75</th>
                <th className="num">LCP p75</th><th className="num">INP p75</th><th className="num">JS errors</th></tr></thead>
              <tbody>{rows(pages.data).map((r) => (
                <tr key={String(r.page)}>
                  <td className="mono" title={String(r.page)}>{String(r.page || "/")}</td>
                  <td className="num">{fmtNum(Number(r.views ?? 0))}</td>
                  <td className="num">{r.load75 == null ? "–" : `${(Number(r.load75) / 1000).toFixed(2)} s`}</td>
                  <td className={`num ${tone("LCP", r.lcp75)}`}>{r.lcp75 == null ? "–" : `${(Number(r.lcp75) / 1000).toFixed(2)} s`}</td>
                  <td className={`num ${tone("INP", r.inp75)}`}>{r.inp75 == null ? "–" : `${Math.round(Number(r.inp75))} ms`}</td>
                  <td className={`num${Number(r.errors) ? " bad" : ""}`}>{fmtNum(Number(r.errors ?? 0))}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </Loads>
      </Panel>

      <div className="grid2">
        <Panel title="JavaScript errors" flush>
          <Loads q={errors} empty={!rows(errors.data).length} height={120}>
            {() => (
              <table className="dtable">
                <thead><tr><th style={{ width: "60%" }}>Error</th><th className="num">Count</th><th className="num">Visits</th><th>Last</th></tr></thead>
                <tbody>{rows(errors.data).map((r) => (
                  <tr key={String(r.message)} onClick={() => r.session && setSession(String(r.session))} title="Open a visit with this error">
                    <td style={{ whiteSpace: "normal", wordBreak: "break-word" }}><div className="bad">{String(r.message)}</div><div className="faint mono">{String(r.page ?? "")}{r.source ? ` · ${r.source}` : ""}</div></td>
                    <td className="num">{fmtNum(Number(r.n))}</td>
                    <td className="num">{fmtNum(Number(r.visits))}</td>
                    <td className="faint">{fmtTime(String(r.last), ctx.range)}</td>
                  </tr>
                ))}</tbody>
              </table>
            )}
          </Loads>
        </Panel>
        <Panel title="Failed or slow API calls" flush right={<span className="faint">from the page; click for the backend trace</span>}>
          <Loads q={calls} empty={!rows(calls.data).length} height={120}>
            {() => (
              <table className="dtable">
                <thead><tr><th style={{ width: "55%" }}>Call</th><th className="num">Status</th><th className="num">Count</th><th className="num">p75</th></tr></thead>
                <tbody>{rows(calls.data).map((r, i) => (
                  <tr key={i} onClick={() => r.trace && ctx.go(`/traces/${r.trace}`)} title={r.trace ? "Open its trace" : "No trace for this call"}>
                    <td className="mono" style={{ whiteSpace: "normal", wordBreak: "break-all" }}>{String(r.method)} {String(r.url)}</td>
                    <td className={`num${Number(r.status) >= 500 || String(r.status) === "0" ? " bad" : ""}`}>{String(r.status) === "0" ? "failed" : String(r.status)}</td>
                    <td className="num">{fmtNum(Number(r.n))}</td>
                    <td className="num">{`${Math.round(Number(r.p75 ?? 0))} ms`}</td>
                  </tr>
                ))}</tbody>
              </table>
            )}
          </Loads>
        </Panel>
      </div>

      <Panel title="Browsers and devices" flush>
        <Loads q={browsers} empty={!rows(browsers.data).length} height={100}>
          {() => (
            <table className="dtable">
              <thead><tr><th>Browser</th><th>Device</th><th className="num">Visits</th><th className="num">JS errors</th></tr></thead>
              <tbody>{rows(browsers.data).map((r, i) => (
                <tr key={i}><td>{String(r.browser ?? "other")}</td><td>{String(r.device ?? "")}</td>
                  <td className="num">{fmtNum(Number(r.visits))}</td><td className={`num${Number(r.errors) ? " bad" : ""}`}>{fmtNum(Number(r.errors))}</td></tr>
              ))}</tbody>
            </table>
          )}
        </Loads>
      </Panel>

      {session && <Visit ctx={ctx} app={app} session={session} w={w} onClose={() => setSession(null)} />}
    </>
  );
}

function tone(name: string, v: unknown) {
  return v == null ? "" : ({ good: "ok", "needs-improvement": "warn", poor: "bad", "": "" } as Record<string, string>)[rate(name, Number(v))];
}

/** One visit (session): its events in order, each with what it measured; API calls link to their traces. */
function Visit({ ctx, app, session, w, onClose }: { ctx: Ctx; app: string; session: string; w: { start: string; end: string }; onClose: () => void }) {
  const ev = useSql(`SELECT ts, severity_text AS sev, body, ${A("rum.type")} AS type, ${A("url.path")} AS page, trace_id,
      resource_attributes['browser.name'] AS browser, resource_attributes['os.name'] AS os, resource_attributes['device.type'] AS device
    FROM logs WHERE ${RUM} AND service = ${q(app)} AND ${A("rum.session_id")} = ${q(session)} ORDER BY ts LIMIT 500`, w, "visit" + session);
  const list = rows(ev.data);
  const first = list[0];
  return (
    <Drawer title={<>Visit <span className="mono faint">{session.slice(0, 12)}</span></>} onClose={onClose}>
      {first && <p className="faint">{String(first.browser ?? "")} on {String(first.os ?? "")} ({String(first.device ?? "")}) · {list.length} events</p>}
      <Loads q={ev} empty={!list.length} height={120}>
        {() => (
          <table className="dtable">
            <tbody>{list.map((r, i) => (
              <tr key={i} onClick={() => r.trace_id && (onClose(), ctx.go(`/traces/${r.trace_id}`))}>
                <td className="faint" style={{ whiteSpace: "nowrap" }}>{fmtTime(String(r.ts), ctx.range)}</td>
                <td className={r.sev === "ERROR" ? "bad" : r.sev === "WARN" ? "warn" : ""}>{String(r.body)}</td>
                <td className="faint mono">{r.trace_id ? "trace ›" : ""}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Loads>
    </Drawer>
  );
}
