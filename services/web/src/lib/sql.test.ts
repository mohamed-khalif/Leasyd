import { test } from "node:test";
import assert from "node:assert/strict";
import { sqlString } from "./sql";

test("a value becomes one string literal, whatever it holds", () => {
  assert.equal(sqlString("checkout"), "'checkout'");
  assert.equal(sqlString("it's"), "'it''s'");
  assert.equal(sqlString("x' OR 1=1 --"), "'x'' OR 1=1 --'");
  assert.equal(sqlString("a\\'b"), "'a\\''b'");   // backslash is an ordinary character in DuckDB strings
  assert.equal(sqlString(""), "''");
});
