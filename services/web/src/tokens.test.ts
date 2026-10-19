// Colours, shadows' colours included, are written only in tokens.css; everything else uses var(--…).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = join(process.cwd(), "src");
const COLOUR = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(\s*\d/g;

test("no colour literals outside tokens.css", () => {
  const found: string[] = [];
  for (const f of readdirSync(SRC, { recursive: true }).map(String)) {
    if (!/\.(css|tsx?)$/.test(f) || f === "tokens.css" || /\.test\.tsx?$/.test(f) || f.startsWith("test/")) continue;
    readFileSync(join(SRC, f), "utf8").split("\n").forEach((line, i) => {
      for (const m of line.matchAll(COLOUR)) {
        // "#…" inside a URL hash ("#/logs") or an HTML entity ("&#8230;") isn't a colour
        if (m[0].startsWith("#") && /[/&]$/.test(line.slice(0, m.index))) continue;
        found.push(`${f}:${i + 1}: ${m[0]}`);
      }
    });
  }
  assert.deepEqual(found, [], "use a token from src/tokens.css instead");
});
