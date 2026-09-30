import type React from "react";
import { useCallback, useEffect, useState } from "react";
import { me } from "./api";
import { idToken, signOut } from "./auth";
import { getConfig } from "./config";
import { Shell } from "./Shell";
import { SignIn } from "./pages/SignIn";
import { Insights } from "./pages/Insights";
import { Usage } from "./pages/Usage";
import { Logs } from "./pages/Logs";
import { Traces } from "./pages/Traces";
import { Metrics } from "./pages/Metrics";
import { Synthetics } from "./pages/Synthetics";
import { Slos } from "./pages/Slos";
import { Alerts } from "./pages/Alerts";
import { QueryBuilder } from "./pages/QueryBuilder";
import { Sql } from "./pages/Sql";
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

  const loadUser = useCallback(async () => {
    if (!(await idToken())) return setUser(null);
    me().then(setUser, () => setUser(null));
  }, []);
  useEffect(() => { loadUser(); }, [loadUser]);

  if (user === undefined) return <div className="state" style={{ height: "100%" }}>Loading…</div>;
  if (user === null || (getConfig().mock && location.search.includes("signin"))) return <SignIn onSignedIn={loadUser} />;

  const [path, query] = hash.split("?");
  const params = new URLSearchParams(query || "");
  const ctx: Ctx = { range, tick, go };
  let page: React.ReactElement, crumb: [string, string];
  if (path.startsWith("/traces")) {
    const id = path.split("/")[2];
    page = <Traces ctx={ctx} traceId={id} />;
    crumb = ["Traces", id ? `${id.slice(0, 16)}…` : "Explorer"];
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
  } else if (path.startsWith("/query")) {
    page = <QueryBuilder ctx={ctx} params={params} />;
    crumb = ["Query data", "Query Builder"];
  } else if (path.startsWith("/sql")) {
    page = <Sql ctx={ctx} />;
    crumb = ["Query data", "SQL"];
  } else if (path.startsWith("/metrics")) {
    page = <Metrics ctx={ctx} params={params} />;
    crumb = ["Metrics", params.get("m") ?? "Explorer"];
  } else if (path.startsWith("/logs")) {
    page = <Logs ctx={ctx} params={params} />;
    crumb = ["Logs", "Explorer"];
  } else if (path.startsWith("/usage")) {
    page = <Usage ctx={ctx} />;
    crumb = ["Dashboards", "Usage & Cost"];
  } else {
    page = <Insights ctx={ctx} />;
    crumb = ["Dashboards", "Home"];
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
