import { Ai } from "./pages/Ai";
import { Settings } from "./pages/Settings";
import type React from "react";
import { useCallback, useEffect, useState } from "react";
import { me } from "./api";
import { idToken, signOut, startDemo } from "./auth";
import { getConfig } from "./config";
import { Shell } from "./Shell";
import { SignIn } from "./pages/SignIn";
import { Insights } from "./pages/Insights";
import { GetStarted } from "./pages/GetStarted";
import { DropRules } from "./pages/DropRules";
import { Usage } from "./pages/Usage";
import { Logs } from "./pages/Logs";
import { Traces } from "./pages/Traces";
import { ServiceMap } from "./pages/ServiceMap";
import { Lambda } from "./pages/Lambda";
import { Web } from "./pages/Web";
import { Metrics } from "./pages/Metrics";
import { Synthetics } from "./pages/Synthetics";
import { Slos } from "./pages/Slos";
import { Alerts } from "./pages/Alerts";
import { QueryBuilder } from "./pages/QueryBuilder";
import { Sql } from "./pages/Sql";
import { Dashboards } from "./pages/Dashboards";
import { RANGES, Range, rangeFromKey } from "./time";

export type Ctx = { range: Range; tick: number; go: (hash: string) => void };

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
  const [range, setRange] = useState<Range>(() => rangeFromKey(safeGet("leasyd.range")) ?? RANGES[1]);
  const [tick, setTick] = useState(0);
  const hash = useHash();
  const go = useCallback((h: string) => { location.hash = h; }, []);

  const [demoError, setDemoError] = useState<string | null>(null);
  const loadUser = useCallback(async () => {
    // #/demo (the website's "Explore the live demo"): sign in to the read-only demo account.
    if (location.hash.startsWith("#/demo")) {
      try {
        signOut();
        await startDemo();
        location.hash = "/";
      } catch (e) {
        setDemoError((e as Error).message);
        location.hash = "/";
        return setUser(null);
      }
    }
    if (!(await idToken())) return setUser(null);
    me().then(setUser, () => setUser(null));
  }, []);
  useEffect(() => { loadUser(); }, [loadUser]);

  if (user === undefined) return <div className="state" style={{ height: "100%" }}>Loading…</div>;
  if (user === null || (getConfig().mock && location.search.includes("signin"))) return <SignIn onSignedIn={loadUser} notice={demoError} />;

  const [path, query] = hash.split("?");
  const params = new URLSearchParams(query || "");
  const ctx: Ctx = { range, tick, go };
  let page: React.ReactElement, crumb: [string, string];
  if (path.startsWith("/traces")) {
    const id = path.split("/")[2];
    page = <Traces ctx={ctx} traceId={id} service={params.get("service") ?? undefined} />;
    crumb = ["Traces", id ? `${id.slice(0, 16)}…` : "Explorer"];
  } else if (path.startsWith("/services")) {
    page = <ServiceMap ctx={ctx} />;
    crumb = ["Services", "Service map"];
  } else if (path.startsWith("/web")) {
    page = <Web ctx={ctx} />;
    crumb = ["Web", "Real users"];
  } else if (path.startsWith("/lambda")) {
    const fn = path.split("/")[2];
    page = <Lambda key={fn ?? ""} ctx={ctx} fn={fn ? decodeURIComponent(fn) : undefined} />;
    crumb = ["AWS Lambda", fn ? decodeURIComponent(fn) : "Functions"];
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
    page = <GetStarted ctx={ctx} />;
    crumb = ["Get started", "Send your first data"];
  } else if (path.startsWith("/drop-rules")) {
    page = <DropRules ctx={ctx} />;
    crumb = ["Account", "Drop rules"];
  } else if (path.startsWith("/settings")) {
    page = <Settings ctx={ctx} />;
    crumb = ["Settings", "Account"];
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
      {page}
    </Shell>
  );
}

function safeGet(k: string): string | null { try { return localStorage.getItem(k); } catch { return null; } }
function safeSet(k: string, v: string) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }
