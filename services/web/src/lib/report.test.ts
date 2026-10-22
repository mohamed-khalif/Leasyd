// The report's numbers and its "top things to fix" from known inputs.
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildReport, meterCost, ReportInputs } from "./report";
import type { Session, Issue } from "./agents";
import type { Svc } from "./services";

const session = (id: string, cost: number | null, errors = 0): Session => ({
  id, single: false, traces: [`t-${id}`], start: 1, end: 2, agent: "support", models: {}, input: 0, output: 0,
  llmCalls: 3, toolCalls: 1, errors, toolErrors: 0, llmErrors: 0, agentErrors: errors, cost, costEstimated: false });
const svc = (service: string, requests: number, errors: number, kind = "service"): Svc => ({
  service, requests, errors, avg_ms: 12, attrs: {}, kind, ebpf: false, sdk: true });
const issue = (title: string, n: number): Issue => ({ key: title, kind: "tool_hidden", subject: "x", title, detail: "",
  sessions: Array.from({ length: n }, (_, i) => `s${i}`), occurrences: n, example: "" });
const base: ReportInputs = {
  company: "Shop", period: { start: "2026-10-15T00:00:00Z", end: "2026-10-22T00:00:00Z", label: "Last 7 days" }, now: new Date(0),
  sessions: null, issues: [], services: null, errorMessages: [], checks: null, meters: null };

test("totals, the most expensive conversations and the worst services", () => {
  const r = buildReport({ ...base,
    sessions: [session("a", 0.5), session("b", 2, 1), session("c", null), session("d", 0.25)],
    services: [svc("checkout", 1000, 30), svc("cart", 2000, 1), svc("redis", 500, 50, "database"), svc("email", 10, 0)],
    errorMessages: [{ service: "checkout", message: "card declined", n: 3 }, { service: "checkout", message: "card expired", n: 20 }] });
  assert.equal(r.agents!.conversations, 4);
  assert.equal(r.agents!.failed, 1);
  assert.equal(r.agents!.cost, 2.75);
  assert.deepEqual(r.agents!.expensive.map((c) => c.id), ["b", "a", "d"]);   // no cost: left out
  assert.equal(r.services!.count, 3);                                         // the database isn't a service of theirs
  assert.equal(r.services!.requests, 3010);
  assert.deepEqual(r.services!.worst.map((w) => [w.service, w.errorPct, w.topError]), [["checkout", 3, "card expired"], ["cart", 0.05, ""]]);
});

test("top things to fix: worst first, at most 3, nothing when all is well", () => {
  const r = buildReport({ ...base,
    services: [svc("checkout", 100, 8), svc("cart", 100, 2)],
    sessions: [session("a", 1)], issues: [issue("check_stock failed, and the agent answered anyway", 4)],
    checks: [{ name: "Homepage", uptimePct: 98.5, runs: 100 }, { name: "Login", uptimePct: 100, runs: 100 }] });
  assert.equal(r.fixes.length, 3, JSON.stringify(r.fixes));
  assert.match(r.fixes[0], /^checkout: 8% of requests failing|^Homepage/);
  const two = buildReport({ ...base, services: [svc("cart", 100, 2)],
    checks: [{ name: "A", uptimePct: 97, runs: 9 }, { name: "B", uptimePct: 98, runs: 9 }, { name: "C", uptimePct: 98.5, runs: 9 }] });
  assert.deepEqual(two.fixes.map((f) => f.split(":")[0]), ["A", "cart", "B"]);   // one of each area first
  assert.ok(r.fixes.some((f) => f.startsWith("AI agents: check_stock failed")), JSON.stringify(r.fixes));
  assert.ok(!r.fixes.some((f) => f.startsWith("Login")));
  const fine = buildReport({ ...base, services: [svc("cart", 100, 0)], checks: [{ name: "Login", uptimePct: 100, runs: 5 }] });
  assert.deepEqual(fine.fixes, []);
});

test("usage: records received, dropped, and their cost at list prices", () => {
  const day = { day: "2026-10-20", in_traces: 2_000_000, dropped_traces: 1_000_000, in_logs: 1_000_000 };
  assert.equal(meterCost(day), 2 * 0.05 + 1 * 0.45 + 1 * 0.05 + 1 * 0.45);
  const r = buildReport({ ...base, meters: [day, { day: "2026-10-21", in_metrics: 1_000_000 }] });
  assert.deepEqual(r.usage, { records: 4_000_000, dropped: 1_000_000, cost: 1.22 });
});
