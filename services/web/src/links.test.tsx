// B5: a malformed or hand-edited link must not blank the app.
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { installMockBackend, inFlight } from "./test/mockBackend";
import { render, settle } from "./test/dom";
import { fmtUnit } from "./time";

before(installMockBackend);

async function idle() {
  for (let quiet = 0, i = 0; quiet < 3 && i < 100; i++) {
    await settle(100);
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}

async function textAt(route: string) {
  location.hash = route;
  const view = await render(<App />);
  await idle();
  await act(async () => { dispatchEvent(new HashChangeEvent("hashchange")); });
  await idle();
  const text = view.container.textContent ?? "";
  await view.unmount();
  return text;
}

test("a Lambda link with a broken %-escape opens the page", async () => {
  assert.match(await textAt("/lambda/%E0%A4%A"), /%E0%A4%A/);
});

for (const [name, state] of [
  ["queries of the wrong shape", { queries: [{ foo: 1 }, null, "x"], settings: {} }],
  ["a builder of the wrong shape", { queries: [{ id: 1, mode: "tracing", b: { filters: "x", groupBy: 5 } }] }],
  ["decimals out of range", { queries: [{ id: 1, mode: "promql", promql: "up" }], settings: { decimals: "500" } }],
] as const) {
  test(`a Query Builder link with ${name} opens the page`, async () => {
    const q = btoa(JSON.stringify(state));
    const text = await textAt(`/query?q=${encodeURIComponent(q)}`);
    assert.match(text, /Query/);
    assert.doesNotMatch(text, /Something went wrong/);
  });
}

test("decimals past what toFixed takes are clamped", () => {
  assert.equal(fmtUnit(1.5, "", 500), (1.5).toFixed(20));
  assert.equal(fmtUnit(1.5, "", -3), "2");
});

test("a page that throws shows an error with a way back, not a blank app", async () => {
  const Boom = () => { throw new Error("kaboom"); };
  const original = console.error;
  console.error = () => undefined;   // React logs the caught error
  try {
    const view = await render(<ErrorBoundary resetKey="a"><Boom /></ErrorBoundary>);
    assert.match(view.container.textContent ?? "", /Something went wrong.*kaboom/);
    await view.rerender(<ErrorBoundary resetKey="b"><p>fine</p></ErrorBoundary>);   // another page: tries again
    assert.equal(view.container.textContent, "fine");
    await view.unmount();
  } finally {
    console.error = original;
  }
});
