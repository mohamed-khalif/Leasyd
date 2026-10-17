// C5: page filters are in the link: a link with new filters to the same page applies them, Back
// restores them, and changing a filter updates the link.
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
const asked = () => bodies.filter((b) => b.path === "/v1/app/query").map((b) => JSON.stringify(b.body));
const follow = (hash: string) => act(async () => { location.hash = hash; dispatchEvent(new HashChangeEvent("hashchange")); });

for (const [page, other] of [["logs", "traces"], ["traces", "logs"]] as const) {
  test(`${page}: a same-page link with another service applies it; Back restores the first`, async () => {
    location.hash = `/${page}?service=checkout`;
    const view = await render(<App />);
    await idle();
    assert.ok(asked().some((q) => q.includes('"services":["checkout"]')), "first service asked");
    bodies.length = 0;
    await follow(`/${page}?service=payments`);
    await idle();
    assert.ok(asked().length > 0 && asked().every((q) => !q.includes("checkout")), "switched to payments");
    assert.ok(asked().some((q) => q.includes('"services":["payments"]')));
    await follow(`/${other}`);
    await idle();
    bodies.length = 0;
    await follow(`/${page}?service=checkout`);                    // what Back does: a hashchange to the old link
    await idle();
    assert.ok(asked().some((q) => q.includes('"services":["checkout"]')), "Back restores the service");
    await view.unmount();
  });
}

test("metrics detail: service and split come from the link and follow it", async () => {
  location.hash = "/metrics?m=http.server.request.duration&service=checkout&by=route";
  const view = await render(<App />);
  await idle();
  assert.ok(asked().some((q) => q.includes('"services":["checkout"]')));
  bodies.length = 0;
  await follow("/metrics?m=http.server.request.duration&service=payments&by=route");
  await idle();
  assert.ok(asked().some((q) => q.includes('"services":["payments"]')));
  await view.unmount();
});

test("setting a filter writes it into the link (no new history entry); the default is left out", async () => {
  const { useHashParam } = await import("./hashParams");
  location.hash = "/logs?range=1h";
  const before = history.length;
  let set!: (v: string) => void;
  function Probe() { const [, s] = useHashParam("service"); set = s; return null; }
  const view = await render(<Probe />);
  await act(async () => set("checkout"));
  assert.equal(location.hash, "#/logs?range=1h&service=checkout");
  await act(async () => set(""));
  assert.equal(location.hash, "#/logs?range=1h");
  assert.equal(history.length, before);
  await view.unmount();
});
