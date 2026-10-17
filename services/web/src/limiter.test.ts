// The query limiter (6 at once per page) with aborts (design §4.1).
import { test } from "node:test";
import assert from "node:assert/strict";
import { limited } from "./limiter";

const flush = () => new Promise<void>((r) => setImmediate(r));

test("an aborted query leaves the queue or frees its place for the next", async () => {
  const started: number[] = [];
  const ends = new Map<number, () => void>();
  const job = (i: number) => (signal?: AbortSignal) => new Promise<number>((resolve, reject) => {
    started.push(i);
    ends.set(i, () => resolve(i));
    signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
  const ctls = Array.from({ length: 9 }, () => new AbortController());
  const ps = ctls.map((c, i) => limited(job(i), c.signal));
  ps.forEach((p) => p.catch(() => undefined));
  await flush();
  assert.deepEqual(started, [0, 1, 2, 3, 4, 5]);
  ctls[6].abort();                                    // queued: leaves the queue without running
  await assert.rejects(ps[6], { name: "AbortError" });
  ctls[0].abort();                                    // running: aborted, its place goes to the next
  await assert.rejects(ps[0], { name: "AbortError" });
  await flush();
  assert.deepEqual(started, [0, 1, 2, 3, 4, 5, 7]);
  ends.get(1)!();
  assert.equal(await ps[1], 1);
  await flush();
  assert.deepEqual(started, [0, 1, 2, 3, 4, 5, 7, 8]);
  [2, 3, 4, 5, 7, 8].forEach((i) => ends.get(i)!());   // leave the limiter empty
  await Promise.all([2, 3, 4, 5, 7, 8].map((i) => ps[i]));
});
