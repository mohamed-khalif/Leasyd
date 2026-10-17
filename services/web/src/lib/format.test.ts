// The examples in docs/formatting.md.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fmtAgo, fmtBytes, fmtCores, fmtDuration, fmtFixed, fmtHours, fmtInt, fmtMiB, fmtMs, fmtNum, fmtPercent, fmtRatio,
         fmtUnitPrice, fmtUsd, NONE } from "./format";

test("numbers", () => {
  assert.deepEqual([950, 12.5, 0.372, 12_345, 1_234_567, 3.4e9].map(fmtNum), ["950", "12.5", "0.372", "12.3K", "1.2M", "3.4B"]);
  assert.equal(fmtInt(1234567), (1234567).toLocaleString());
  assert.equal(fmtFixed(0.1234, 3), "0.123");
  assert.equal(fmtFixed(1.5, 500), (1.5).toFixed(20));
  assert.equal(fmtInt(null), NONE);
});

test("percent: 2 decimals below 1% and from 90%, else 1; never rounds up to 100%; no trailing zeros", () => {
  assert.deepEqual([0, 0.42, 3.5, 45.31, 12, 98.96, 99.95, 99.999, 100].map((v) => fmtPercent(v)),
                   ["0%", "0.42%", "3.5%", "45.3%", "12%", "98.96%", "99.95%", "99.999%", "100%"]);
  assert.equal(fmtPercent(99.98765, 3), "99.988%");
  assert.equal(fmtRatio(0.9996), "99.96%");
  assert.equal(fmtPercent(NaN), NONE);
  assert.equal(fmtPercent(undefined), NONE);
});

test("durations", () => {
  assert.deepEqual([0.42, 1.5, 15].map((ms) => fmtMs(ms * 1e6)), ["0.42 ms", "1.5 ms", "15 ms"]);
  assert.deepEqual([850, 1234, 4 * 60_000, 2.5 * 3_600_000, 86_400_000 * 1.2, 3 * 86_400_000].map(fmtDuration),
                   ["850 ms", "1.23 s", "4 min", "2.5 h", "28.8 h", "3 days"]);
  assert.deepEqual([0.75, 12.5, 1250].map(fmtHours), ["45 min", "12.5 h", `${(1250).toLocaleString()} h`]);
});

test("how long ago", () => {
  const now = Date.UTC(2026, 9, 17, 12);
  assert.deepEqual([5e3, 45e3, 12 * 60e3, 5 * 3600e3, 3 * 86400e3].map((d) => fmtAgo(now - d, now)),
                   ["just now", "45 s ago", "12 min ago", "5 h ago", "3 days ago"]);
  assert.equal(fmtAgo(new Date(now - 86_400_000 * 1.6).toISOString(), now), "2 days ago");
  assert.equal(fmtAgo(null), NONE);
});

test("sizes, CPU and money", () => {
  assert.deepEqual([512 * 2 ** 20, 1.5 * 2 ** 30, 2 ** 30].map(fmtBytes), ["512 MiB", "1.5 GiB", "1 GiB"]);
  assert.equal(fmtMiB(16384), "16 GiB");
  assert.deepEqual([0.25, 1.5, 12].map(fmtCores), ["250m", "1.50 cores", "12.0 cores"]);
  assert.deepEqual([0.42, 12, 1234].map(fmtUsd), ["$0.42", "$12", `$${(1234).toLocaleString()}`]);
  assert.deepEqual([0.18, 0.075, 3.43].map(fmtUnitPrice), ["$0.18", "$0.075", "$3.43"]);
});
