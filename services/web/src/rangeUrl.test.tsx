// C4: the time range is in the link, so a shared link opens with the sender's range.
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { bodies, installMockBackend, inFlight } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);

async function idle() {
  for (let quiet = 0, i = 0; quiet < 3 && i < 100; i++) {
    await settle(100);
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}
/** Minutes spanned by the query windows sent since `bodies` was cleared. */
const spans = () => new Set(bodies.filter((b) => b.path === "/v1/app/query").map((b) => b.body as { start?: string; end?: string })
  .filter((b) => b.start && b.end).map((b) => Math.round((Date.parse(b.end!) - Date.parse(b.start!)) / 60_000)));

test("a link with range=1h opens with the last hour, without asking with another range first", async () => {
  location.hash = "/logs?range=1h";
  bodies.length = 0;
  const view = await render(<App />);
  await idle();
  assert.deepEqual([...spans()], [60]);
  assert.match(view.container.textContent ?? "", /Last 1 hour/);
  await view.unmount();
});

test("a page opened without a range gets the current one written into its link", async () => {
  location.hash = "/traces";
  const view = await render(<App />);
  await idle();
  assert.match(location.hash, /^#\/traces\?(.*&)?range=[a-z0-9-]+/);
  await view.unmount();
});

test("following a link (or Back) to another range switches to it", async () => {
  location.hash = "/logs?range=1h";
  const view = await render(<App />);
  await idle();
  bodies.length = 0;
  await act(async () => { location.hash = "/logs?range=24h"; dispatchEvent(new HashChangeEvent("hashchange")); });
  await idle();
  assert.deepEqual([...spans()], [1440]);
  await view.unmount();
});

test("a custom range survives in the link; a bad one is ignored", async () => {
  const from = Date.UTC(2026, 9, 1, 8), to = Date.UTC(2026, 9, 1, 10);
  location.hash = `/logs?range=c${from}-${to}`;
  bodies.length = 0;
  let view = await render(<App />);
  await idle();
  assert.deepEqual([...spans()], [120]);
  await view.unmount();
  location.hash = "/logs?range=nonsense";
  view = await render(<App />);
  await idle();
  assert.ok((view.container.textContent ?? "").length > 50);
  assert.doesNotMatch(location.hash, /range=nonsense/);
  await view.unmount();
});
