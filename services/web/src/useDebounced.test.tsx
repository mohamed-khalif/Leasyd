import "./test/dom";
import { test } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { useDebounced } from "./useDebounced";
import { render } from "./test/dom";

test("follows the value 400 ms after it stops changing; flush() takes it at once", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const seen: string[] = [];
  let flush!: () => void;
  function Probe({ v }: { v: string }) { const [s, f] = useDebounced(v); flush = f; seen.push(s); return null; }
  const view = await render(<Probe v="a" />);
  await view.rerender(<Probe v="ab" />);
  await act(async () => t.mock.timers.tick(300));
  await view.rerender(<Probe v="abc" />);
  await act(async () => t.mock.timers.tick(300));
  assert.equal(seen.at(-1), "a", "still typing");
  await act(async () => t.mock.timers.tick(100));
  assert.equal(seen.at(-1), "abc");
  await view.rerender(<Probe v="abcd" />);
  await act(async () => flush());
  assert.equal(seen.at(-1), "abcd", "Enter: at once");
  await view.unmount();
});
