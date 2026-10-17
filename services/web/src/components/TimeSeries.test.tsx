// B4: a line chart over a big result must draw, not throw (Math.min(...points) overflowed the stack).
import "../test/dom";
import { test } from "node:test";
import assert from "node:assert/strict";
import { render } from "../test/dom";
import { TimeSeries } from "./TimeSeries";
import { customRange } from "../time";

test("a chart over 300k points renders", async () => {
  const t0 = Date.UTC(2026, 9, 1);
  const series = Array.from({ length: 30 }, (_, i) => ({
    label: `s${i}`, color: "red", points: Array.from({ length: 10_000 }, (_, j) => [t0 + j * 1000, i + j] as [number, number]),
  }));
  const view = await render(<TimeSeries series={series} range={customRange(t0, t0 + 10_000_000)} />);
  assert.ok(view.container.querySelector("svg"));
  await view.unmount();
});

test("hovering doesn't redraw the lines or move the time axis", async (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: Date.UTC(2026, 9, 17, 12) });
  const { act } = await import("react");
  const { RANGES } = await import("../time");
  const t0 = Date.now() - 10 * 60_000;
  const series = [{ label: "a", color: "red", points: Array.from({ length: 50 }, (_, j) => [t0 + j * 10_000, j] as [number, number]) }];
  const view = await render(<TimeSeries series={series} range={RANGES[0]} />);
  const d = () => view.container.querySelector("path[fill='none']")!.getAttribute("d");
  const before = d();
  t.mock.timers.tick(60_000);                          // a minute later
  await act(async () => { view.container.querySelector("svg")!.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 200 })); });
  assert.equal(d(), before);
  await view.unmount();
});
