// One shared "now" per page: every panel on a page asks for the same window, so their numbers agree.
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
const ends = () => new Set(bodies.filter((b) => b.path === "/v1/app/query").map((b) => (b.body as { end?: string }).end).filter(Boolean));

for (const route of ["/", "/services", "/kubernetes", "/dashboards/builtin-services", "/slos"]) {
  test(`every query on ${route} uses one window end`, async () => {
    location.hash = route;
    bodies.length = 0;
    const view = await render(<App />);
    await idle();
    assert.ok(ends().size >= 1);
    assert.equal(ends().size, 1, `window ends: ${[...ends()].join(", ")}`);
    await view.unmount();
  });
}

test("Refresh and moving to another page each take a new now", async () => {
  location.hash = "/logs";
  const view = await render(<App />);
  await idle();
  const first = [...ends()].pop();
  await settle(20);
  bodies.length = 0;
  await act(async () => { location.hash = "/traces"; dispatchEvent(new HashChangeEvent("hashchange")); });
  await idle();
  const second = [...ends()];
  assert.equal(second.length, 1);
  assert.notEqual(second[0], first, "a new page, a new now");
  await settle(20);
  bodies.length = 0;
  const refresh = view.container.querySelector<HTMLButtonElement>('button[aria-label="Refresh"]')!;
  await act(async () => { refresh.click(); });
  await idle();
  const third = [...ends()];
  assert.equal(third.length, 1);
  assert.notEqual(third[0], second[0], "Refresh, a new now");
  await view.unmount();
});
