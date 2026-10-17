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
