// Alerts: a rule's list row never reads "ok" when Leasyd can't tell: a firing alert without data
// says "no data", and a rule that can't be evaluated says so.
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { inFlight, installMockBackend } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);

async function idle() {
  for (let quiet = 0, i = 0; quiet < 3 && i < 100; i++) {
    await settle(100);
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}

test("the rules list shows no data and can't evaluate, never ok, for those rules", async () => {
  location.hash = "/alerts";
  const view = await render(<App />);
  await idle();
  await act(async () => { dispatchEvent(new HashChangeEvent("hashchange")); });
  await idle();
  const rows = [...view.container.querySelectorAll("tr")].map((r) => r.textContent ?? "");
  const row = (name: string) => rows.find((t) => t.includes(name)) ?? "";
  assert.match(row("Checkout is down"), /firing \(1\).*no data/);
  assert.match(row("Checkout budget"), /can't evaluate/);
  assert.doesNotMatch(row("Checkout budget"), /ok$/);
  await view.unmount();
});
