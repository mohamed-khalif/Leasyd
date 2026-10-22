// The live demo's door: #/demo asks for a work email, sends it with the session request, and opens
// the demo account on the AI agents page with the demo banner.
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { bodies, installMockBackend } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);

test("#/demo takes an email, then opens the AI agents page read-only", async () => {
  sessionStorage.clear();
  location.hash = "/demo";
  const view = await render(<App />);
  await settle(300);
  const text = () => view.container.textContent ?? "";
  assert.match(text(), /Explore the live demo/);
  const input = view.container.querySelector<HTMLInputElement>("input[type=email]")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "cto@shop.io");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => { view.container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
  for (let i = 0; i < 30 && !text().includes("live demo:"); i++) await settle(100);
  assert.deepEqual(bodies.find((b) => b.path === "/v1/demo/session")?.body, { email: "cto@shop.io" });
  assert.match(location.hash, /^#\/agents(\?|$)/);
  assert.match(text(), /You're exploring a live demo/);
  await view.unmount();
  sessionStorage.clear();
});
