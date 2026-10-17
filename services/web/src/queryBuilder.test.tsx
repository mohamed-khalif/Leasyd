// B1: in the Query Builder, editing one query while another is still running must not leave the
// other stuck on "loading".
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { useResults, type Q } from "./pages/QueryBuilder";
import { answer, calls, install, type Answer } from "./test/fetchStub";
import { deferred, render, settle } from "./test/dom";

before(install);

const W = { start: "2026-10-17T00:00:00Z", end: "2026-10-17T01:00:00Z" };
const query = (id: number): Q => ({ id, name: "", mode: "promql", promql: "", b: { measure: "rate", filters: [], groupBy: [], metric: "", agg: "sum" } });
const matrix = (v: string) => ({ status: 200, body: { status: "success", data: { resultType: "matrix", result: [{ metric: {}, values: [[1, v]] }] } } });

function Probe(p: { texts: string[]; seen: unknown[] }) {
  p.seen.push(useResults([query(1), query(2)], p.texts, W, 60, "0"));
  return null;
}

test("a query still running when another is edited gets its answer", async () => {
  const pending = new Map<string, ReturnType<typeof deferred<Answer>>>();
  answer((c) => {
    const d = deferred<Answer>();
    pending.set((c.body as { promql: string }).promql, d);
    return d.promise;
  });
  const seen: unknown[] = [];
  const view = await render(<Probe texts={["up", "a"]} seen={seen} />);
  await view.rerender(<Probe texts={["up", "ab"]} seen={seen} />);   // typing in query 2 while query 1 runs
  pending.get("up")!.resolve(matrix("1"));
  pending.get("a")!.resolve(matrix("2"));
  pending.get("ab")!.resolve(matrix("3"));
  await settle();
  const [first, second] = seen[seen.length - 1] as { loading: boolean; data: unknown[] | null }[];
  assert.equal(first.loading, false, "query 1 is not stuck loading");
  assert.equal(first.data?.length, 1);
  assert.equal(second.loading, false);
  assert.equal(calls.filter((c) => (c.body as { promql: string }).promql === "up").length, 1, "query 1 ran once");
  await view.unmount();
});
