// Settings: one page per topic under #/settings/<page>; API keys can be named; a return from
// Stripe lands on Billing; a page's done note isn't shown on another page.
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { bodies, inFlight, installMockBackend } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);

async function idle() {
  for (let quiet = 0, i = 0; quiet < 3 && i < 100; i++) {
    await settle(100);
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}

async function open(route: string) {
  location.hash = route;
  const view = await render(<App />);
  await idle();
  await act(async () => { dispatchEvent(new HashChangeEvent("hashchange")); });
  await idle();
  return view;
}

const go = async (route: string) => { await act(async () => { location.hash = route; dispatchEvent(new HashChangeEvent("hashchange")); }); await idle(); };

test("the menu lists every page and marks the open one", async () => {
  const view = await open("/settings/team");
  const links = [...view.container.querySelectorAll(".set-menu a")].map((a) => a.textContent);
  assert.deepEqual(links, ["Profile", "Security", "Organization", "Team", "Usage & cost", "Billing", "API keys", "GPU prices", "AI tools"]);
  assert.equal(view.container.querySelector(".set-menu a.active")?.textContent, "Team");
  assert.match(view.container.textContent ?? "", /Invite someone/);
  await view.unmount();
});

test("API keys: a key is created with its name, and the list shows names", async () => {
  const view = await open("/settings/api-keys");
  const c = view.container;
  assert.match(c.textContent ?? "", /prod ECS/);   // the mock's named key
  const name = c.querySelector<HTMLInputElement>('input[aria-label="Key name"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(name, "  staging api ");
    name.dispatchEvent(new Event("input", { bubbles: true }));
  });
  bodies.length = 0;
  await act(async () => { c.querySelector<HTMLFormElement>(".set-form")!.requestSubmit(); });
  await idle();
  assert.deepEqual(bodies.find((b) => b.path.endsWith("/account/keys"))?.body, { scope: "ingest", name: "staging api" });
  assert.match(c.textContent ?? "", /New key “staging api”: copy it now/);
  assert.match(c.querySelector(".dtable")?.textContent ?? "", /staging api/);
  await view.unmount();
});

test("back from Stripe at the old address: Billing with the thank-you note, which other pages don't show", async () => {
  const view = await open("/settings?billing=done");
  assert.equal(view.container.querySelector(".set-menu a.active")?.textContent, "Billing");
  assert.match(view.container.textContent ?? "", /payment details are saved/);
  assert.match(view.container.textContent ?? "", /This month so far/);
  await go("/settings/profile");
  assert.doesNotMatch(view.container.textContent ?? "", /payment details are saved/);
  await view.unmount();
});

test("Billing shows the trial's days left", async () => {
  const view = await open("/settings/billing");
  assert.match(view.container.querySelector(".set-notice")?.textContent ?? "", /Free trial: \d+ days? left/);
  await view.unmount();
});

test("the old Usage & Cost address opens Settings › Usage & cost, with today's limits and the 30-day estimate", async () => {
  const view = await open("/usage");
  const text = view.container.textContent ?? "";
  assert.equal(view.container.querySelector(".set-menu a.active")?.textContent, "Usage & cost");
  assert.match(text, /Data received today/);
  assert.match(text, /Estimated total, next 30 days/);
  assert.equal(document.querySelector('.nav a[href="#/usage"]'), null, "no separate sidebar link");
  await view.unmount();
});
