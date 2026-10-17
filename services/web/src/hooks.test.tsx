// The data hooks as they behave today (Phase 5 safety net): useQuery / useSql (useQuery.ts) and
// useHint / useRows (infra/useInfra.ts). Phase 6 collapses them into one core hook; these tests
// say what must not change (or, where marked, what Phase 6 is meant to change).
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { useQuery, useSql, type Loaded } from "./useQuery";
import { useHint, useRows } from "./infra/useInfra";
import type { Query } from "./api";
import { discovery, type Q } from "./infra/queries";
import { answer, calls, install, type Answer } from "./test/fetchStub";
import { deferred, render, settle } from "./test/dom";

const W = { start: "2026-10-17T00:00:00Z", end: "2026-10-17T01:00:00Z" };
const q = (signal: Query["signal"]): Query => ({ signal, ...W, aggs: [{ fn: "count" }] });
const rows = (n: number) => ({ status: 200, body: { columns: ["n"], rows: [[n]] } });

before(install);

function Probe(p: { use: () => unknown; seen: unknown[] }) {
  p.seen.push(p.use());
  return null;
}
const last = <T,>(seen: unknown[]) => seen[seen.length - 1] as T;

test("useQuery: loading, then the data", async () => {
  const d = deferred<Answer>();
  answer(() => d.promise);
  const seen: unknown[] = [];
  await render(<Probe seen={seen} use={() => useQuery(q("logs"), "k1")} />);
  assert.deepEqual(last<Loaded>(seen), { data: null, error: null, loading: true });
  d.resolve(rows(5));
  await settle();
  assert.deepEqual(last<Loaded>(seen), { data: { columns: ["n"], rows: [[5]] }, error: null, loading: false });
});

test("useQuery: an error clears the data and says why", async () => {
  answer(() => ({ status: 400, body: { error: "bad query: x" } }));
  const seen: unknown[] = [];
  await render(<Probe seen={seen} use={() => useQuery(q("logs"), "k1")} />);
  await settle();
  assert.deepEqual(last<Loaded>(seen), { data: null, error: "bad query: x", loading: false });
});

test("useQuery: on a new key the old data stays while loading, then is replaced", async () => {
  let n = 0;
  const next = deferred<Answer>();
  answer(() => (++n === 1 ? rows(1) : next.promise));
  const seen: unknown[] = [];
  const view = await render(<Probe seen={seen} use={() => useQuery(q("logs"), "k1")} />);
  await settle();
  await view.rerender(<Probe seen={seen} use={() => useQuery(q("traces"), "k2")} />);
  assert.deepEqual(last<Loaded>(seen), { data: { columns: ["n"], rows: [[1]] }, error: null, loading: true });
  next.resolve(rows(2));
  await settle();
  assert.deepEqual(last<Loaded>(seen), { data: { columns: ["n"], rows: [[2]] }, error: null, loading: false });
});

test("useQuery: an answer to an older key is ignored", async () => {
  const slow = deferred<Answer>(), fast = deferred<Answer>();
  answer((c) => ((c.body as Query).signal === "logs" ? slow.promise : fast.promise));
  const seen: unknown[] = [];
  const view = await render(<Probe seen={seen} use={() => useQuery(q("logs"), "k1")} />);
  await view.rerender(<Probe seen={seen} use={() => useQuery(q("traces"), "k2")} />);
  fast.resolve(rows(2));
  await settle();
  slow.resolve(rows(1));                 // the first key's answer arrives last
  await settle();
  assert.deepEqual(last<Loaded>(seen).data, { columns: ["n"], rows: [[2]] });
});

test("useQuery: no query, no request", async () => {
  answer(() => rows(1));
  const seen: unknown[] = [];
  await render(<Probe seen={seen} use={() => useQuery(null, "k")} />);
  await settle();
  assert.equal(calls.length, 0);
  assert.deepEqual(last<Loaded>(seen), { data: null, error: null, loading: false });
});

test("today: a query that becomes null while loading stays 'loading' (code-audit §2.1)", async () => {
  const d = deferred<Answer>();
  answer(() => d.promise);
  const seen: unknown[] = [];
  const view = await render(<Probe seen={seen} use={() => useQuery(q("logs"), "k1")} />);
  await view.rerender(<Probe seen={seen} use={() => useQuery(null, "k2")} />);
  d.resolve(rows(1));
  await settle();
  assert.equal(last<Loaded>(seen).loading, true);
});

test("useSql: sends the SQL over the window, keyed like useQuery", async () => {
  answer(() => rows(3));
  const seen: unknown[] = [];
  await render(<Probe seen={seen} use={() => useSql("SELECT count(*) AS n FROM logs", W, "s1")} />);
  await settle();
  assert.deepEqual(calls[0].body, { sql: "SELECT count(*) AS n FROM logs", ...W });
  assert.deepEqual(last<Loaded>(seen).data, { columns: ["n"], rows: [[3]] });
});

test("useHint + useRows: data waits for discovery on first load and reads only the services found", async () => {
  const disc = deferred<Answer>();
  answer((c) => ((c.body as { sql: string }).sql === discovery().sql ? disc.promise : rows(7)));
  const data: Q = { uses: ["gpu"], sql: "SELECT 1 AS n FROM metrics" };
  const seen: unknown[] = [];
  function Page({ k }: { k: string }) {
    const hint = useHint(W, k);
    seen.push(useRows<{ n: number }>(data, W, k, hint));
    return null;
  }
  const view = await render(<Page k="a" />);
  await settle();
  assert.equal(calls.length, 1);                         // only discovery so far
  disc.resolve({ status: 200, body: { columns: ["kind", "service", "n"], rows: [["gpu", "dcgm-exporter", 9]] } });
  await settle();
  assert.deepEqual(calls[1].body, { sql: data.sql, ...W, services: ["dcgm-exporter"] });
  assert.deepEqual(last<{ rows: unknown[] }>(seen).rows, [{ n: 7 }]);
  // today (code-audit C6): on a new key the data query runs at once with the previous hint,
  // alongside the new discovery, instead of waiting for it.
  calls.length = 0;
  await view.rerender(<Page k="b" />);
  await settle();
  assert.deepEqual(calls.map((c) => (c.body as { sql: string }).sql === discovery().sql ? "discovery" : "data").sort(), ["data", "discovery"]);
});
