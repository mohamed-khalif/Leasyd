// Get started: each source shows its own setup; AWS logs points at the CloudWatch Logs stack.
import "./test/dom";
import { test, before } from "node:test";
import { act } from "react";
import assert from "node:assert/strict";
import { App } from "./App";
import { installMockBackend, inFlight } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);

async function idle() {
  for (let quiet = 0, i = 0; quiet < 3 && i < 100; i++) {
    await settle(100);
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}

test("AWS logs shows the CloudWatch Logs stack, with the ingest endpoint for logs", async () => {
  location.hash = "/start?source=aws-logs";
  const view = await render(<App />);
  await idle();
  const tab = [...view.container.querySelectorAll("[role=tab]")].find((b) => b.textContent?.includes("AWS logs (CloudWatch)"));
  assert.equal(tab?.getAttribute("aria-selected"), "true");
  const code = [...view.container.querySelectorAll("pre.code")].map((p) => p.textContent ?? "").join("\n");
  assert.match(code, /install\/cloudwatch-logs\.yaml/);
  assert.match(code, /CAPABILITY_AUTO_EXPAND/);
  assert.match(code, /LeasydEndpoint=\S+\/v1\/aws\/cloudwatch-logs/);
  assert.match(code, /LogGroups=/);
  await view.unmount();
});

test("sources are grouped, every source is offered, and the steps tick off as they're done", async () => {
  location.hash = "/start";
  const view = await render(<App />);
  await idle();
  const c = view.container;
  assert.match(c.querySelector(".start-hero")?.textContent ?? "", /any cloud, server or app/);
  assert.deepEqual([...c.querySelectorAll(".start-group h4")].map((h) => h.firstChild?.textContent?.trim()),
    ["Your apps", "Infrastructure", "Websites", "Cloud integrations"]);
  assert.equal(c.querySelectorAll("[role=tab]").length, 11);
  // The mock account has data and searchable services: steps 2 and 3 are done; no key yet, so step 1 isn't.
  const done = [...c.querySelectorAll(".start-stepcard")].map((s) => s.classList.contains("done"));
  assert.deepEqual(done, [false, true, true]);
  const input = c.querySelector<HTMLInputElement>("input[aria-label='Your API key']")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "obs_test");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  assert.ok(c.querySelector(".start-stepcard")?.classList.contains("done"), "pasting a key ticks step 1");
  await view.unmount();
});
