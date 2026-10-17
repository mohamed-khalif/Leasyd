// B13: a panel whose result was cut at the row cap says so.
import "../test/dom";
import { test } from "node:test";
import assert from "node:assert/strict";
import { render } from "../test/dom";
import { Loads } from "./Panel";
import { loads } from "../infra/useInfra";

const data = (truncated: boolean) => ({ columns: ["n"], rows: [[1]], truncated });

test("a truncated result shows a notice under the panel; a whole one doesn't", async () => {
  const cut = await render(<Loads q={{ data: data(true), error: null, loading: false }}>{() => <p>rows</p>}</Loads>);
  assert.match(cut.container.textContent ?? "", /rows.*Not everything is shown/);
  await cut.unmount();
  const whole = await render(<Loads q={{ data: data(false), error: null, loading: false }}>{() => <p>rows</p>}</Loads>);
  assert.equal(whole.container.textContent, "rows");
  await whole.unmount();
});

test("Kubernetes / GPU rows carry the cut through to the panel", async () => {
  const view = await render(<Loads q={loads({ rows: [], error: null, loading: false, truncated: true })}>{() => <p>rows</p>}</Loads>);
  assert.match(view.container.textContent ?? "", /Not everything is shown/);
  await view.unmount();
});
