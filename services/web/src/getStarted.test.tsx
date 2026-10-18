// Get started: each source shows its own setup; AWS logs points at the CloudWatch Logs stack.
import "./test/dom";
import { test, before } from "node:test";
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
