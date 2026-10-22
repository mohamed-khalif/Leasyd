// The report page against the mock backend: it builds, shares a snapshot, and the shared link opens
// that snapshot with no sign-in.
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { bodies, installMockBackend } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);

test("the report shares a snapshot that opens from its link without signing in", async () => {
  location.hash = "/report?range=7d";
  const view = await render(<App />);
  const text = () => view.container.textContent ?? "";
  for (let i = 0; i < 60 && !text().includes("Top things to fix"); i++) await settle(100);
  assert.match(text(), /Reliability & cost report/);
  assert.match(text(), /Leasyd cost/);
  const share = [...view.container.querySelectorAll("button")].find((b) => b.textContent === "Share link")!;
  for (let i = 0; i < 30 && share.disabled; i++) await settle(100);
  await act(async () => { share.click(); });
  for (let i = 0; i < 30 && !text().includes("Link copied"); i++) await settle(100);
  const sent = bodies.find((b) => b.path === "/v1/app/account/reports")?.body as { report: { company: string; fixes: string[] } };
  assert.ok(sent && sent.report.company && Array.isArray(sent.report.fixes));
  const url = view.container.querySelector<HTMLAnchorElement>(".form-note a")!.getAttribute("href")!;
  await view.unmount();

  location.hash = url.slice(url.indexOf("#") + 1);
  const shared = await render(<App />);
  const st = () => shared.container.textContent ?? "";
  for (let i = 0; i < 30 && !st().includes(sent.report.company); i++) await settle(100);
  assert.match(st(), /Shared from Leasyd/);
  assert.match(st(), new RegExp(sent.report.company));
  assert.doesNotMatch(st(), /Sign out/);           // not the signed-in app
  await shared.unmount();
});
