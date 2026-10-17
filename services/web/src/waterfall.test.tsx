// The trace waterfall draws 500 span rows at a time; finding a span and expand/collapse all cover
// the whole trace.
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { answer, install } from "./test/fetchStub";
import { render, settle } from "./test/dom";

before(install);

const T0 = Date.UTC(2026, 9, 17, 12) * 1e6;
const span = (id: string, parent: string | null, name: string, i: number) =>
  ({ span_id: id, parent_span_id: parent, name, service: "api", ts_unix_nano: T0 + i * 1e6, duration_ns: 1e6, status_code: 0, events: [] });
const SPANS = [span("root", null, "GET /checkout", 0),
  ...Array.from({ length: 1200 }, (_, i) => span(`s${i}`, "root", `op-${i}`, i + 1)),
  span("deep", "s1100", "charge-card", 1102)];

test("1,202 spans: 500 drawn, search reaches the end, collapse/expand all act on the whole tree", async () => {
  answer((c) => {
    if (c.path === "/v1/app/me") return { status: 200, body: { tenant: "acme", email: "a@x" } };
    if (c.method === "POST") {
      const q = c.body as { signal: string };
      const rows = q.signal === "traces" ? SPANS : [];
      const cols = rows.length ? Object.keys(rows[0]) : [];
      return { status: 200, body: { columns: cols, rows: rows.map((r) => cols.map((k) => (r as Record<string, unknown>)[k])) } };
    }
    return { status: 404, body: {} };
  });
  location.hash = "/traces/abc123";
  const view = await render(<App />);
  for (let i = 0; i < 30 && !view.container.querySelector(".tw-row:not(.tw-headrow)"); i++) await settle(50);
  const rows = () => view.container.querySelectorAll(".tw-row:not(.tw-headrow)").length;
  const text = () => view.container.textContent ?? "";
  assert.equal(rows(), 500);
  assert.match(text(), /Showing 500 of 1,202/);

  const box = view.container.querySelector<HTMLInputElement>('input[aria-label="Find a span"]')!;
  const type = (v: string) => act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(box, v);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await type("charge-card");
  assert.match(text(), /1 found/);
  assert.equal(rows(), 3, "the match with its parent and the root");
  assert.match(text(), /charge-card/);
  await type("");

  const click = (label: string) => act(async () => [...view.container.querySelectorAll("button")].find((b) => b.textContent === label)!.click());
  await click("Collapse all");
  assert.equal(rows(), 1, "only the root");
  await click("Expand all");
  assert.equal(rows(), 500);
  await view.unmount();
});
