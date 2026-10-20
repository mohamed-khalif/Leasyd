import type React from "react";
import { lazy, startTransition, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, me } from "./api";
import { idToken, signOut } from "./auth";
import { isIdle, useIdleSignOut } from "./idle";
import { getConfig } from "./config";
import { CONTACT_EMAIL, Shell } from "./Shell";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { SignIn } from "./pages/SignIn";
import { settingsPage } from "./pages/settings/menu";
import { RANGES, Range, rangeFromKey } from "./time";
import { hashParams, replaceHashParams } from "./hashParams";

// Each page's code is its own chunk, loaded when the page is first opened, or, for the page a link
// opens, at start-up next to the sign-in check (preloadPage). A page whose code is already here
// renders at once: through React.lazy it would show the fallback first, and React then holds the
// page back for at least 300 ms (its Suspense reveal throttle) on every first visit.
type Page<P> = ((props: P) => React.ReactElement) & { preload: () => Promise<unknown> };
function splitPage<P extends object>(load: () => Promise<React.ComponentType<P>>): Page<P> {
  let done: React.ComponentType<P> | undefined, loading: Promise<React.ComponentType<P>> | undefined;
  const preload = () => (loading ??= load().then((c) => (done = c)));
  const Lazy = lazy(() => preload().then((c) => ({ default: c })));
  const Show = (props: P) => { const C = done ?? Lazy; return <C {...props} />; };
  return Object.assign(Show, { preload });
}
const Ai = splitPage(() => import("./pages/Ai").then((m) => m.Ai));
const Settings = splitPage(() => import("./pages/Settings").then((m) => m.Settings));
const Insights = splitPage(() => import("./pages/Insights").then((m) => m.Insights));
const GetStarted = splitPage(() => import("./pages/GetStarted").then((m) => m.GetStarted));
const DropRules = splitPage(() => import("./pages/DropRules").then((m) => m.DropRules));
const Logs = splitPage(() => import("./pages/Logs").then((m) => m.Logs));
const Traces = splitPage(() => import("./pages/Traces").then((m) => m.Traces));
const ServiceMap = splitPage(() => import("./pages/ServiceMap").then((m) => m.ServiceMap));
const Lambda = splitPage(() => import("./pages/Lambda").then((m) => m.Lambda));
const Agents = splitPage(() => import("./pages/Agents").then((m) => m.Agents));
const Web = splitPage(() => import("./pages/Web").then((m) => m.Web));
const Gpus = splitPage(() => import("./pages/Gpus").then((m) => m.Gpus));
const Kubernetes = splitPage(() => import("./pages/Kubernetes").then((m) => m.Kubernetes));
const Metrics = splitPage(() => import("./pages/Metrics").then((m) => m.Metrics));
const Synthetics = splitPage(() => import("./pages/Synthetics").then((m) => m.Synthetics));
const Slos = splitPage(() => import("./pages/Slos").then((m) => m.Slos));
const Alerts = splitPage(() => import("./pages/Alerts").then((m) => m.Alerts));
const QueryBuilder = splitPage(() => import("./pages/QueryBuilder").then((m) => m.QueryBuilder));
const Sql = splitPage(() => import("./pages/Sql").then((m) => m.Sql));
const Dashboards = splitPage(() => import("./pages/Dashboards").then((m) => m.Dashboards));

/** The page each path opens, in the same order as App's routes (the last is Home). */
const ROUTE_PAGES: [string, Page<never>][] = [["/traces", Traces], ["/services", ServiceMap], ["/web", Web], ["/gpus", Gpus],
  ["/kubernetes", Kubernetes], ["/lambda", Lambda], ["/agents", Agents], ["/synthetics", Synthetics], ["/slos", Slos], ["/alerts", Alerts],
  ["/dashboards", Dashboards], ["/query", QueryBuilder], ["/sql", Sql], ["/metrics", Metrics], ["/logs", Logs], ["/ai", Ai],
  ["/start", GetStarted], ["/drop-rules", DropRules], ["/settings", Settings], ["/usage", Settings]];
export const preloadPage = (hash: string) =>
  (ROUTE_PAGES.find(([prefix]) => hash.startsWith(prefix))?.[1] ?? (Insights as Page<never>)).preload().catch(() => undefined);

/** range: the time range; tick: bumped by Refresh; now: the page's one "now" (epoch ms) that its
 *  windows end at, set when the range changes, on Refresh and on opening a page. */
export type Ctx = { range: Range; tick: number; now: number; go: (hash: string) => void };

function useHash(): string {
  const [hash, setHash] = useState(() => {
    const h = location.hash.slice(1) || "/";
    preloadPage(h);                              // the opened page's code, alongside config and the sign-in check
    return h;
  });
  useEffect(() => {
    // A transition: the current page stays up while the next page's code loads (no fallback to hold back).
    const on = () => { const h = location.hash.slice(1) || "/"; preloadPage(h); startTransition(() => setHash(h)); };
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
    if (isIdle()) {   // back after a break: signed out before anything loads
      signOut();
      setNotice("You were signed out after 15 minutes without activity. Sign in again to continue.");
      return setUser(null);
    }
    if (!(await idToken())) return setUser(null);
    // Signed out only when the API says so; any other failure is shown, with Retry.
    me().then(setUser, (e: Error) => (e instanceof ApiError && (e.status === 401 || e.status === 403) ? setUser(null) : setMeError(e.message)));
  }, []);
  useEffect(() => { loadUser(); }, [loadUser]);
  const idle = useCallback(() => {
    signOut();
    setUser(null);
    setNotice("You were signed out after 15 minutes without activity. Sign in again to continue.");
  }, []);
  useIdleSignOut(!!user, idle);

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
  } else if (path.startsWith("/agents")) {
    page = <Agents ctx={ctx} />;
    crumb = ["Applications", "AI agents"];
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
    crumb = ["Get started", "Connect a data source"];
  } else if (path.startsWith("/drop-rules")) {
    page = <DropRules ctx={ctx} />;
    crumb = ["Account", "Drop rules"];
  } else if (path.startsWith("/settings") || path.startsWith("/usage")) {   // #/usage: the old Usage & Cost page
    const at = path.startsWith("/usage") ? "/settings/usage" : path;
    page = <Settings ctx={ctx} path={at} />;
    crumb = ["Settings", settingsPage(at).label];
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
