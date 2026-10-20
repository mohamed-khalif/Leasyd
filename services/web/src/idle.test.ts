// Idle sign-out (idle.ts): 15 minutes without activity in any tab signs the user out.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./test/dom";
import { IDLE_MS, isIdle, lastActive, markActive } from "./idle";

test("idle only after 15 minutes without activity; no record is not idle", () => {
  localStorage.clear();
  assert.equal(isIdle(Date.now()), false);                 // first visit: nothing recorded yet
  const t = 1_800_000_000_000;
  markActive(t);
  assert.equal(lastActive(), t);
  assert.equal(isIdle(t + IDLE_MS), false);
  assert.equal(isIdle(t + IDLE_MS + 1), true);
  markActive(t + IDLE_MS);                                  // activity in another tab counts
  assert.equal(isIdle(t + IDLE_MS + 1), false);
  assert.equal(IDLE_MS, 15 * 60 * 1000);
});
