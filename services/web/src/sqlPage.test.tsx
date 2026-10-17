// B15: on the SQL page, Ctrl+Enter while a query runs doesn't start another (their answers raced).
import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { installMockBackend, inFlight, requests } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);

test("Ctrl+Enter twice runs the query once", async () => {
  location.hash = "/sql";
  const view = await render(<App />);
  for (let i = 0; i < 20 && !view.container.querySelector("textarea"); i++) await settle(100);
  const box = view.container.querySelector("textarea")!;
  requests.length = 0;
  const ctrlEnter = () => box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }));
  await act(async () => { ctrlEnter(); });
  await act(async () => { ctrlEnter(); });
  for (let i = 0; i < 30 && inFlight > 0; i++) await settle(100);
  assert.equal(requests.filter((r) => r === "POST /v1/app/query").length, 1);
  await view.unmount();
});
