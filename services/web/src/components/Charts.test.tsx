// While a new range loads, pages still draw the old rows, whose time column (ts:<old bucket>) no
// longer matches: bar times come out NaN. Those bars are skipped, not drawn at NaN.
import "../test/dom";
import { test } from "node:test";
import assert from "node:assert/strict";
import { render } from "../test/dom";
import { StackedBars } from "./Charts";
import { RANGES } from "../time";

test("bars without a valid time are skipped", async () => {
  const t = Date.now() - 60_000;
  const view = await render(<StackedBars bars={[{ t: NaN, values: [3] }, { t, values: [2] }]} keys={[{ label: "n", color: "red" }]}
                                         range={RANGES[0]} bucketMs={30_000} />);
  const html = view.container.innerHTML;
  assert.doesNotMatch(html, /NaN/);
  assert.equal(view.container.querySelectorAll("rect").length, 1);
  await view.unmount();
});

