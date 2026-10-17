// node:test over csv.ts (run: npm test).
import assert from "node:assert/strict";
import { test } from "node:test";
import { toCsv } from "./csv";

test("plain values, quoting, nulls and objects", () => {
  assert.equal(toCsv(["a", "b"], [[1, "x,y"], [null, { k: 1 }], ["say \"hi\"", "two\nlines"]]),
    'a,b\n1,"x,y"\n,"{""k"":1}"\n"say ""hi""","two\nlines"');
});

test("text that a spreadsheet would run as a formula is made inert (B12)", () => {
  for (const bad of ["=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\tcmd", "\rcmd"]) {
    const out = toCsv(["c"], [[bad]]).split("\n").slice(1).join("\n");
    assert.ok(/^"?'/.test(out), `${JSON.stringify(bad)} -> ${JSON.stringify(out)}`);
  }
  // A header can come from a SQL alias, so it is guarded too.
  assert.ok(toCsv(["=cmd"], []).startsWith("'="));
});

test("numbers stay numbers, negatives included", () => {
  assert.equal(toCsv(["n"], [[-5], [0.5], [-1e-3]]), "n\n-5\n0.5\n-0.001");
});
