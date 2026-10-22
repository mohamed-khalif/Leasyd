// The Playground: #/playground signs straight in to the demo account (no sign-up) and opens the AI
// agents page with the Playground banner.
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { App } from "./App";
import { installMockBackend, requests } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);

test("#/playground opens the AI agents page read-only, with no sign-up", async () => {
  sessionStorage.clear();
  location.hash = "/playground";
  const view = await render(<App />);
  const text = () => view.container.textContent ?? "";
  for (let i = 0; i < 30 && !text().includes("Playground:"); i++) await settle(100);
  assert.ok(requests.some((r) => r.startsWith("POST /v1/demo/session")), "asked for a Playground session");
  assert.match(location.hash, /^#\/agents(\?|$)/);
  assert.match(text(), /You're in the Playground/);
  assert.doesNotMatch(text(), /Work email/);
  await view.unmount();
  sessionStorage.clear();
});
