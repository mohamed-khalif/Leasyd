import "../test/dom";
import { test } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { render } from "../test/dom";
import { Capped } from "./Capped";

test("draws 500 rows, says how many there are, and shows 500 more per click", async () => {
  const rows = Array.from({ length: 1234 }, (_, i) => i);
  const view = await render(<Capped rows={rows}>{(shown) => <ul>{shown.map((r) => <li key={r}>{r}</li>)}</ul>}</Capped>);
  const count = () => view.container.querySelectorAll("li").length;
  assert.equal(count(), 500);
  assert.match(view.container.textContent ?? "", /Showing 500 of 1,234/);
  await act(async () => view.container.querySelector("button")!.click());
  assert.equal(count(), 1000);
  await act(async () => view.container.querySelector("button")!.click());
  assert.equal(count(), 1234);
  assert.equal(view.container.querySelector("button"), null);
  await view.unmount();
});

test("short lists are drawn whole with no footer", async () => {
  const view = await render(<Capped rows={[1, 2, 3]}>{(shown) => <ul>{shown.map((r) => <li key={r}>{r}</li>)}</ul>}</Capped>);
  assert.equal(view.container.querySelectorAll("li").length, 3);
  assert.doesNotMatch(view.container.textContent ?? "", /Showing/);
  await view.unmount();
});
