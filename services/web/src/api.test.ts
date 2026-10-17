// api.ts as it behaves today (Phase 5 safety net): error messages, the session check, and the
// long-query flow (202 + job id -> poll every 2 s, give up after 6 minutes). Where today's
// behaviour is a known bug (docs/code-audit.md), the test says so; the fix changes the test.
import { test, before, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { getConfig } from "./config";
import { account, ApiError, query, sql } from "./api";
import { answer, calls, install } from "./test/fetchStub";

const Q = { signal: "logs" as const, start: "2026-10-17T00:00:00Z", end: "2026-10-17T01:00:00Z", aggs: [{ fn: "count" }] };

before(install);
beforeEach(() => { getConfig().mock = true; });

test("a query is POSTed with the session's token and answered inline", async () => {
  answer(() => ({ status: 200, body: { columns: ["count"], rows: [[7]] } }));
  assert.deepEqual(await query(Q), { columns: ["count"], rows: [[7]] });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].path, "/v1/app/query");
  assert.deepEqual(calls[0].body, Q);
  assert.equal(calls[0].headers.Authorization, "mock-token");
});

test("errors carry the API's message, else a status message", async () => {
  const cases: [number, unknown, string][] = [
    [400, { error: "unknown field 'x'" }, "unknown field 'x'"],
    [413, { error: "result too large" }, "result too large"],
    [429, { error: "too many queries" }, "too many queries"],
    [503, { message: "Leasyd is temporarily unavailable" }, "Leasyd is temporarily unavailable"],
    [500, undefined, "Request failed (500)."],
    [504, undefined, "The query took too long. Try a shorter time range."],
    [502, "<html>bad gateway</html>", "Request failed (502)."],        // not JSON
  ];
  for (const [status, body, message] of cases) {
    answer(() => ({ status, body }));
    await assert.rejects(query(Q), (e: unknown) => e instanceof ApiError && e.status === status && e.message === message);
  }
  // REST calls map errors the same way.
  answer(() => ({ status: 403, body: { error: "you are not a user of this account" } }));
  await assert.rejects(account.get(), (e: unknown) => e instanceof ApiError && e.status === 403 && e.message === "you are not a user of this account");
});

test("today: a network failure surfaces as fetch's own error (not mapped; code-audit C11)", async () => {
  answer(() => new TypeError("Failed to fetch"));
  await assert.rejects(query(Q), (e: unknown) => e instanceof TypeError && e.message === "Failed to fetch");
});

test("no session: refused before any request", async () => {
  getConfig().mock = false;      // a real Cognito pool, nobody signed in
  answer(() => ({ status: 200, body: {} }));
  await assert.rejects(query(Q), (e: unknown) => e instanceof ApiError && e.status === 401 && /session has ended/.test(e.message));
  await assert.rejects(account.get(), (e: unknown) => e instanceof ApiError && e.status === 401);
  assert.equal(calls.length, 0);
});

test("a long query: 202 + job, polled every 2 s until its answer; onWait hears the seconds", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 });
  let polls = 0;
  answer((c) => c.method === "POST" ? { status: 202, body: { job: "a".repeat(32), status: "running" } }
    : ++polls < 3 ? { status: 202, body: { job: "a".repeat(32), status: "running" } } : { status: 200, body: { columns: ["n"], rows: [[1]] } });
  const waited: number[] = [];
  const p = sql({ sql: "SELECT 1 FROM logs", start: Q.start, end: Q.end }, { onWait: (s) => waited.push(s) });
  for (let i = 0; i < 5; i++) { await flush(); t.mock.timers.tick(2000); }
  assert.deepEqual(await p, { columns: ["n"], rows: [[1]] });
  assert.deepEqual(calls.map((c) => `${c.method} ${c.path}`), ["POST /v1/app/query", ...Array(3).fill(`GET /v1/app/query/${"a".repeat(32)}`)]);
  assert.deepEqual(waited, [0, 2, 4]);
});

test("background asks for a job from the start", async () => {
  answer(() => ({ status: 200, body: { columns: [], rows: [] } }));
  await sql({ sql: "SELECT 1 FROM logs", start: Q.start, end: Q.end }, { background: true });
  assert.deepEqual(calls[0].body, { sql: "SELECT 1 FROM logs", start: Q.start, end: Q.end, async: true });
});

test("a job still running after 6 minutes: the client gives up with 504", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 });
  answer(() => ({ status: 202, body: { job: "b".repeat(32), status: "running" } }));
  const p = query(Q);
  const failed = assert.rejects(p, (e: unknown) => e instanceof ApiError && e.status === 504 && /too long/.test(e.message));
  for (let i = 0; i < 200; i++) { await flush(); t.mock.timers.tick(2000); }
  await failed;
  assert.ok(calls.length >= 180 && calls.length <= 182, `polled ${calls.length} times`);   // every 2 s for 6 minutes
});

test("today: a 202 without a job id fails at once as 'took too long' (code-audit §2.1)", async () => {
  answer(() => ({ status: 202, body: "not json" }));
  await assert.rejects(query(Q), (e: unknown) => e instanceof ApiError && e.status === 504);
});

test("a transient 503 or network error while polling is retried; the job's answer comes back (B8)", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 });
  let polls = 0;
  answer((c) => {
    if (c.method === "POST") return { status: 202, body: { job: "c".repeat(32) } };
    polls++;
    return polls === 1 ? { status: 503, body: { error: "try again" } } : polls === 2 ? new TypeError("Failed to fetch")
      : { status: 200, body: { columns: ["n"], rows: [[7]] } };
  });
  const p = query(Q);
  for (let i = 0; i < 4; i++) { await flush(); t.mock.timers.tick(2000); }
  assert.deepEqual(await p, { columns: ["n"], rows: [[7]] });
});

test("polling that keeps failing ends the wait with the error (B8)", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 });
  answer((c) => c.method === "POST" ? { status: 202, body: { job: "c".repeat(32) } } : { status: 503, body: { error: "try again" } });
  const p = query(Q);
  const failed = assert.rejects(p, (e: unknown) => e instanceof ApiError && e.status === 503);
  for (let i = 0; i < 6; i++) { await flush(); t.mock.timers.tick(2000); }
  await failed;
});

test("a job's own error (not transient) ends the wait at once", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 });
  answer((c) => c.method === "POST" ? { status: 202, body: { job: "c".repeat(32) } } : { status: 400, body: { error: "bad query: x" } });
  const p = query(Q);
  const failed = assert.rejects(p, (e: unknown) => e instanceof ApiError && e.status === 400);
  await flush(); t.mock.timers.tick(2000);
  await failed;
});

const flush = () => new Promise<void>((r) => setImmediate(r));
