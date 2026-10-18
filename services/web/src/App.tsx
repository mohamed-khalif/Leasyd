import type React from "react";
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, me } from "./api";
import { idToken, signOut } from "./auth";
import { getConfig } from "./config";
import { CONTACT_EMAIL, Shell } from "./Shell";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { SignIn } from "./pages/SignIn";
import { settingsPage } from "./pages/settings/menu";
import { RANGES, Range, rangeFromKey } from "./time";
import { hashParams, replaceHashParams } from "./hashParams";

// Each page's code loads when it is first opened (one chunk per page).
const Ai = lazy(() => import("./pages/Ai").then((m) => ({ default: m.Ai })));
const Settings = lazy(() => import("./pages/Settings").then((m) => ({ default: m.Settings })));
const Insights = lazy(() => import("./pages/Insights").then((m) => ({ default: m.Insights })));
const GetStarted = lazy(() => import("./pages/GetStarted").then((m) => ({ default: m.GetStarted })));
const DropRules = lazy(() => import("./pages/DropRules").then((m) => ({ default: m.DropRules })));
const Usage = lazy(() => import("./pages/Usage").then((m) => ({ default: m.Usage })));
const Logs = lazy(() => import("./pages/Logs").then((m) => ({ default: m.Logs })));
const Traces = lazy(() => import("./pages/Traces").then((m) => ({ default: m.Traces })));
const ServiceMap = lazy(() => import("./pages/ServiceMap").then((m) => ({ default: m.ServiceMap })));
const Lambda = lazy(() => import("./pages/Lambda").then((m) => ({ default: m.Lambda })));
const Web = lazy(() => import("./pages/Web").then((m) => ({ default: m.Web })));
const Gpus = lazy(() => import("./pages/Gpus").then((m) => ({ default: m.Gpus })));
const Kubernetes = lazy(() => import("./pages/Kubernetes").then((m) => ({ default: m.Kubernetes })));
const Metrics = lazy(() => import("./pages/Metrics").then((m) => ({ default: m.Metrics })));
const Synthetics = lazy(() => import("./pages/Synthetics").then((m) => ({ default: m.Synthetics })));
const Slos = lazy(() => import("./pages/Slos").then((m) => ({ default: m.Slos })));
const Alerts = lazy(() => import("./pages/Alerts").then((m) => ({ default: m.Alerts })));
const QueryBuilder = lazy(() => import("./pages/QueryBuilder").then((m) => ({ default: m.QueryBuilder })));
const Sql = lazy(() => import("./pages/Sql").then((m) => ({ default: m.Sql })));
const Dashboards = lazy(() => import("./pages/Dashboards").then((m) => ({ default: m.Dashboards })));

/** range: the time range; tick: bumped by Refresh; now: the page's one "now" (epoch ms) that its
 *  windows end at, set when the range changes, on Refresh and on opening a page. */
export type Ctx = { range: Range; tick: number; now: number; go: (hash: string) => void };

function useHash(): string {
  const [hash, setHash] = useState(location.hash.slice(1) || "/");
  useEffect(() => {
    const on = () => setHash(location.hash.slice(1) || "/");
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function App() {
  const [user, setUser] = useState<{ tenant: string; email: string } | null | undefined>(undefined);
  // The range: from the link (shared links open with the sender's range), else the last one picked here.
  const [range, setRange] = useState<Range>(() => rangeFromKey(hashParams().get("range")) ?? rangeFromKey(safeGet("leasyd.range")) ?? RANGES[1]);
  const [tick, setTick] = useState(0);
  const hash = useHash();
  const [rangeFor, setRangeFor] = useState(hash);
  if (rangeFor !== hash) {                       // followed a link or Back: take the link's range, if it has one
    setRangeFor(hash);
    const linked = rangeFromKey(hashParams(hash).get("range"));
    if (linked && linked.key !== range.key) setRange(linked);
  }
  useEffect(() => {                              // keep the link's range current (and drop a bad one)
    if (hashParams().get("range") !== range.key) replaceHashParams({ range: range.key });
  }, [hash, range.key]);
  const [now, setNow] = useState(() => Date.now());
  const [nowFor, setNowFor] = useState({ path: hash.split("?")[0], range, tick });
  if (nowFor.path !== hash.split("?")[0] || nowFor.range !== range || nowFor.tick !== tick) {
    setNowFor({ path: hash.split("?")[0], range, tick });   // adjusting state on a change, during render
    setNow(Date.now());
  }
  const go = useCallback((h: string) => { location.hash = h; }, []);

  const [notice, setNotice] = useState<string | null>(null);
  const [meError, setMeError] = useState<string | null>(null);
  const loadUser = useCallback(async () => {
    setMeError(null);
    // #/demo: the old live demo's link (closed); point to booking one
    if (location.hash.startsWith("#/demo")) {
      setNotice(`Our self-serve demo has closed. Book a demo and we'll walk you through Leasyd: ${CONTACT_EMAIL}`);
      location.hash = "/";
    }
    if (!(await idToken())) return setUser(null);
    // Signed out only when the API says so; any other failure is shown, with Retry.
    me().then(setUser, (e: Error) => (e instanceof ApiError && (e.status === 401 || e.status === 403) ? setUser(null) : setMeError(e.message)));
  }, []);
  useEffect(() => { loadUser(); }, [loadUser]);

  const ctx: Ctx = useMemo(() => ({ range, tick, now, go }), [range, tick, now, go]);   // the same object until one changes

  if (meError) return (
    <div className="state error" style={{ height: "100%" }}>
      Couldn't reach Leasyd: {meError} <button className="btn small" onClick={() => { setUser(undefined); loadUser(); }}>Retry</button>
    </div>);
  if (user === undefined) return <div className="state" style={{ height: "100%" }}>Loading…</div>;
  if (user === null || (getConfig().mock && location.search.includes("signin"))) return <SignIn onSignedIn={loadUser} notice={notice} />;

  const [path, query] = hash.split("?");
  const params = new URLSearchParams(query || "");
  let page: React.ReactElement, crumb: [string, string];
  if (path.startsWith("/traces")) {
    const id = path.split("/")[2];
    page = <Traces ctx={ctx} traceId={id} />;
    crumb = ["Traces", id ? `${id.slice(0, 16)}…` : "Explorer"];
  } else if (path.startsWith("/services")) {
    page = <ServiceMap ctx={ctx} />;
    crumb = ["Services", "Service map"];
  } else if (path.startsWith("/web")) {
    page = <Web ctx={ctx} />;
    crumb = ["Web", "Real users"];
  } else if (path.startsWith("/gpus")) {
    page = <Gpus key={query ?? ""} ctx={ctx} params={params} />;   // a link with ?gpu= or ?tab= opens fresh
    crumb = ["Infrastructure", "GPUs"];
  } else if (path.startsWith("/kubernetes")) {
    page = <Kubernetes key={query ?? ""} ctx={ctx} params={params} />;
    crumb = ["Infrastructure", "Kubernetes"];
  } else if (path.startsWith("/lambda")) {
    const fn = path.split("/")[2];
    const name = fn ? decoded(fn) : undefined;
    page = <Lambda key={fn ?? ""} ctx={ctx} fn={name} />;
    crumb = ["AWS Lambda", name ?? "Functions"];
  } else if (path.startsWith("/synthetics")) {
    page = <Synthetics ctx={ctx} path={path} />;
    const sub = path.split("/")[2];
    crumb = ["Synthetics", sub === "windows" ? "Maintenance windows" : sub === "new" ? "New check" : sub ? (path.endsWith("/edit") ? "Edit check" : "Check") : "Checks"];
  } else if (path.startsWith("/slos")) {
    page = <Slos ctx={ctx} path={path} />;
    const sub = path.split("/")[2];
    crumb = ["SLOs", sub === "new" ? "New SLO" : sub ? (path.endsWith("/edit") ? "Edit SLO" : "SLO") : "All SLOs"];
  } else if (path.startsWith("/alerts")) {
    page = <Alerts ctx={ctx} path={path} params={params} />;
    const [, , tab, sub] = path.split("/");
    crumb = ["Alerts", tab === "rules" && sub ? (sub === "new" ? "New rule" : "Rule") : tab === "channels" ? "Channels" : tab === "history" ? "History" : "Rules"];
  } else if (path.startsWith("/dashboards")) {
    page = <Dashboards ctx={ctx} path={path} params={params} />;
    crumb = ["Dashboards", path.endsWith("/edit") ? "Edit dashboard" : "All dashboards"];
  } else if (path.startsWith("/query")) {
    page = <QueryBuilder ctx={ctx} params={params} />;
    crumb = ["Analyze", "Query Builder"];
  } else if (path.startsWith("/sql")) {
    page = <Sql ctx={ctx} />;
    crumb = ["Analyze", "SQL"];
  } else if (path.startsWith("/metrics")) {
    page = <Metrics ctx={ctx} params={params} />;
    crumb = ["Metrics", params.get("m") ?? "Explorer"];
  } else if (path.startsWith("/logs")) {
    page = <Logs ctx={ctx} params={params} />;
    crumb = ["Logs", "Explorer"];
  } else if (path.startsWith("/ai")) {
    page = <Ai ctx={ctx} path={path} params={params} />;
    crumb = ["Leasyd AI", path.split("/")[2] ? "Investigation" : "Ask"];
  } else if (path.startsWith("/start")) {
    page = <GetStarted ctx={ctx} initial={params.get("source")} />;
    crumb = ["Get started", "Send your first data"];
  } else if (path.startsWith("/drop-rules")) {
    page = <DropRules ctx={ctx} />;
    crumb = ["Account", "Drop rules"];
  } else if (path.startsWith("/settings")) {
    page = <Settings ctx={ctx} path={path} />;
    crumb = ["Settings", settingsPage(path).label];
  } else if (path.startsWith("/usage")) {
    page = <Usage ctx={ctx} />;
    crumb = ["Account", "Usage & Cost"];
  } else {
    page = <Insights ctx={ctx} />;
    crumb = ["Leasyd", "Home"];
  }

  return (
    <Shell path={path} crumb={crumb} user={user} range={range}
           onRange={(r) => { setRange(r); safeSet("leasyd.range", r.key); }}
           onRefresh={() => setTick((t) => t + 1)}
           onSignOut={() => { signOut(); setUser(null); }}>
      <ErrorBoundary resetKey={hash}><Suspense fallback={<div className="skeleton" style={{ height: 300 }} />}>{page}</Suspense></ErrorBoundary>
    </Shell>
  );
}

function safeGet(k: string): string | null { try { return localStorage.getItem(k); } catch { return null; } }
function safeSet(k: string, v: string) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

/** A link's %-escaped part, or the text as is when it isn't valid (a hand-edited link). */
function decoded(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}
