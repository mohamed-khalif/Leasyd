// At most 6 queries in flight at once per page (Home asks about 20).
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { App } from "./App";
import { installMockBackend, inFlight } from "./test/mockBackend";
import { render, settle } from "./test/dom";

let now = 0, peak = 0, total = 0;
before(async () => {
  await installMockBackend();
  const inner = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const isQuery = String(input).endsWith("/v1/app/query") && init?.method === "POST";
    if (isQuery) { now++; total++; peak = Math.max(peak, now); }
    try { return await inner(input, init); } finally { if (isQuery) now--; }
  }) as typeof fetch;
});

test("Home's ~20 queries run at most 6 at a time, and all of them run", async () => {
  location.hash = "/";
  const view = await render(<App />);
  for (let quiet = 0, i = 0; quiet < 3 && i < 100; i++) { await settle(100); quiet = inFlight === 0 ? quiet + 1 : 0; }
  assert.ok(total >= 15, `queries asked: ${total}`);
  assert.equal(peak, 6);
  await view.unmount();
});
