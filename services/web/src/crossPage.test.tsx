// Phase 7: a range or Refresh chosen on one page carries to the others. Pages are left through the
// sidebar's own links (which carry no range), as a user would.
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
type Window = { start?: string; end?: string };
// Kubernetes/GPU discovery looks at the last 15 minutes of the window on purpose (infra/useInfra.ts): not a range query.
const isDiscovery = (b: unknown) => /^SELECT 'gpu' AS kind/.test(String((b as { sql?: string }).sql ?? ""));
const windows = () => bodies.filter((b) => b.path === "/v1/app/query" && !isDiscovery(b.body)).map((b) => b.body as Window).filter((w) => w.start && w.end);
const spans = () => new Set(windows().map((w) => Math.round((Date.parse(w.end!) - Date.parse(w.start!)) / 60_000)));
const ends = () => new Set(windows().map((w) => Date.parse(w.end!)));

/** Follow the sidebar link to `path`, as a click would (the browser sets the hash). */
async function navTo(c: HTMLElement, path: string) {
  const a = c.querySelector<HTMLAnchorElement>(`.nav a[href="#${path}"]`);
  assert.ok(a, `sidebar link to ${path}`);
  bodies.length = 0;
  await act(async () => { location.hash = a.getAttribute("href")!; dispatchEvent(new HashChangeEvent("hashchange")); });
  await idle();
}

const label = (c: HTMLElement) => c.querySelector('button[aria-haspopup="listbox"]')?.textContent ?? "";

test("a range picked on one page is used by every page reached from the sidebar, and by a fresh load", async () => {
  location.hash = "/logs?range=15m";
  let view = await render(<App />);
  await idle();
  const c = view.container;
  await act(async () => { c.querySelector<HTMLButtonElement>('button[aria-haspopup="listbox"]')!.click(); });
  const sixHours = [...c.querySelectorAll<HTMLButtonElement>('[role="option"]')].find((b) => b.textContent === "Last 6 hours")!;
  bodies.length = 0;
  await act(async () => { sixHours.click(); });
  await idle();
  assert.deepEqual([...spans()], [360], "Logs asks again with the new range");

  for (const path of ["/traces", "/metrics", "/", "/kubernetes", "/services"]) {
    await navTo(c, path);
    assert.deepEqual([...spans()], [360], `${path} uses the range picked on Logs`);
    assert.equal(ends().size, 1, `${path}: one window end`);
    assert.match(location.hash, /[?&]range=6h(&|$)/, `${path}: the link carries the range`);
    assert.match(label(c), /Last 6 hours/);
  }
  await view.unmount();

  location.hash = "/traces";              // a new tab or reload, link without a range: the last one picked
  bodies.length = 0;
  view = await render(<App />);
  await idle();
  assert.deepEqual([...spans()], [360]);
  await view.unmount();
});

test("after Refresh on one page, the next page's windows end no earlier than the Refresh, with the same range", async () => {
  location.hash = "/traces?range=1h";
  const view = await render(<App />);
  await idle();
  const c = view.container;
  const before = Math.max(...ends());
  await settle(30);
  const pressed = Date.now();
  bodies.length = 0;
  await act(async () => { c.querySelector<HTMLButtonElement>('button[aria-label="Refresh"]')!.click(); });
  await idle();
  const refreshed = [...ends()];
  assert.equal(refreshed.length, 1, `refreshed ends: ${refreshed}`);
  assert.ok(refreshed[0] >= pressed && refreshed[0] > before, "Refresh takes a new now on this page");

  await navTo(c, "/logs");
  const next = [...ends()];
  assert.equal(next.length, 1, `logs ends: ${next.map((t) => new Date(t).toISOString())}`);
  assert.ok(next[0] >= refreshed[0], "the next page is no older than the Refresh");
  assert.deepEqual([...spans()], [60]);
  await view.unmount();
});
