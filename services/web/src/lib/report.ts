// The reliability & cost report (pages/Report.tsx): one page over a time range, built from the same
// data as the AI agents, APM, synthetic checks and billing pages, and shared as a snapshot (this
// Report object, kept by the account API behind an expiring link). Pure: tested in report.test.ts.
import type { Issue, Session } from "./agents";
import type { Svc } from "./services";
import type { Meter } from "../api";
import { PRICE } from "../pricing";
import { fmtPercent } from "./format";

export type Report = {
  version: 1;
  company: string;
  period: { start: string; end: string; label: string };
  generated_at: string;
  agents: { conversations: number; failed: number; cost: number | null; costEstimated: boolean;
            issues: { title: string; conversations: number }[];
            expensive: { id: string; agent: string; cost: number; llmCalls: number; start: number; trace: string }[] } | null;
  services: { count: number; requests: number; errors: number;
              worst: { service: string; requests: number; errorPct: number; avgMs: number; topError: string }[] } | null;
  uptime: { checks: { name: string; uptimePct: number; runs: number }[] } | null;
  usage: { records: number; dropped: number; cost: number } | null;
  fixes: string[];
};

export type ReportInputs = {
  company: string; period: Report["period"]; now: Date;
  sessions: Session[] | null; issues: Issue[];
  services: Svc[] | null;
  errorMessages: { service: string; message: string; n: number }[];
  checks: { name: string; uptimePct: number; runs: number }[] | null;
  meters: Meter[] | null;
};

const SIGNALS = ["traces", "logs", "metrics"] as const;
const pct = (part: number, all: number) => (all ? (part / all) * 100 : 0);
const round = (v: number, d = 2) => Math.round(v * 10 ** d) / 10 ** d;

/** What a Meter day costs at list prices: ingest on everything received, storage on what was kept. */
export function meterCost(d: Meter): number {
  return SIGNALS.reduce((a, s) => {
    const inn = d[`in_${s}`] ?? 0, kept = inn - (d[`dropped_${s}`] ?? 0);
    return a + (inn / 1e6) * PRICE[s].ingest + (kept / 1e6) * PRICE[s].storage;
  }, 0);
}

export function buildReport(x: ReportInputs): Report {
  const agents = x.sessions && x.sessions.length ? {
    conversations: x.sessions.length,
    failed: x.sessions.filter((s) => s.errors > 0).length,
    cost: x.sessions.some((s) => s.cost != null) ? round(x.sessions.reduce((a, s) => a + (s.cost ?? 0), 0), 4) : null,
    costEstimated: x.sessions.some((s) => s.costEstimated),
    issues: x.issues.slice(0, 5).map((i) => ({ title: i.title, conversations: i.sessions.length })),
    expensive: x.sessions.filter((s) => (s.cost ?? 0) > 0).sort((a, b) => (b.cost ?? 0) - (a.cost ?? 0)).slice(0, 5)
      .map((s) => ({ id: s.id, agent: s.agent, cost: round(s.cost ?? 0, 4), llmCalls: s.llmCalls, start: s.start, trace: s.traces[0] ?? "" })),
  } : null;

  const own = (x.services ?? []).filter((s) => s.kind === "service" && s.requests > 0);
  const topError = new Map<string, string>();
  for (const e of [...x.errorMessages].sort((a, b) => b.n - a.n)) if (e.message && !topError.has(e.service)) topError.set(e.service, e.message);
  const services = own.length ? {
    count: own.length,
    requests: own.reduce((a, s) => a + s.requests, 0),
    errors: own.reduce((a, s) => a + s.errors, 0),
    worst: own.filter((s) => s.errors > 0).sort((a, b) => pct(b.errors, b.requests) - pct(a.errors, a.requests)).slice(0, 3)
      .map((s) => ({ service: s.service, requests: s.requests, errorPct: round(pct(s.errors, s.requests)),
                     avgMs: round(s.avg_ms, 1), topError: topError.get(s.service) ?? "" })),
  } : null;

  const uptime = x.checks && x.checks.length ? { checks: [...x.checks].sort((a, b) => a.uptimePct - b.uptimePct) } : null;

  const usage = x.meters && x.meters.length ? {
    records: x.meters.reduce((a, d) => a + SIGNALS.reduce((b, s) => b + (d[`in_${s}`] ?? 0), 0), 0),
    dropped: x.meters.reduce((a, d) => a + SIGNALS.reduce((b, s) => b + (d[`dropped_${s}`] ?? 0), 0), 0),
    cost: round(x.meters.reduce((a, d) => a + meterCost(d), 0)),
  } : null;

  return { version: 1, company: x.company, period: x.period, generated_at: x.now.toISOString(),
           agents, services, uptime, usage, fixes: fixes(agents, services, uptime) };
}

/** The 3 things most worth fixing, by rule (no AI): services with errors, agents' silent failures and
 *  checks below 99.9% uptime, worst first (5%+ errors or under 99% uptime first); the worst of each
 *  area before a second of any, so the 3 cover what's wrong rather than one area. */
function fixes(agents: Report["agents"], services: Report["services"], uptime: Report["uptime"]): string[] {
  const out: [number, string, string][] = [];
  for (const s of services?.worst ?? []) {
    if (s.errorPct < 1) continue;
    out.push([s.errorPct >= 5 ? 0 : 2, "services", `${s.service}: ${fmtPercent(s.errorPct)} of requests failing${s.topError ? `, mostly "${s.topError}"` : ""}.`]);
  }
  for (const i of agents?.issues ?? []) out.push([1, "agents", `AI agents: ${i.title} (${i.conversations} conversation${i.conversations === 1 ? "" : "s"}).`]);
  for (const c of uptime?.checks ?? []) if (c.uptimePct < 99.9) out.push([c.uptimePct < 99 ? 0 : 3, "uptime", `${c.name}: ${fmtPercent(c.uptimePct)} uptime.`]);
  const sorted = out.sort((a, b) => a[0] - b[0]), firsts: typeof out = [], seen = new Set<string>();
  for (const f of sorted) if (!seen.has(f[1])) { seen.add(f[1]); firsts.push(f); }
  return [...firsts, ...sorted.filter((f) => !firsts.includes(f))].slice(0, 3).map(([, , t]) => t);
}

