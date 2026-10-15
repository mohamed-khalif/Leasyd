// Runs the web app's unit tests (src/**/*.test.ts) with node:test, bundled by esbuild.
import { build } from "esbuild";
import { readdirSync, mkdtempSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";

const src = new URL("../src/", import.meta.url).pathname;
const tests = readdirSync(src, { recursive: true }).filter((f) => String(f).endsWith(".test.ts")).map((f) => join(src, String(f)));
const out = mkdtempSync(join(tmpdir(), "leasyd-web-tests-"));
await build({ entryPoints: tests, bundle: true, platform: "node", format: "esm", outdir: out, outExtension: { ".js": ".mjs" }, logLevel: "warning" });
const files = readdirSync(out, { recursive: true }).filter((f) => String(f).endsWith(".mjs")).map((f) => join(out, String(f)));
const r = spawnSync(process.execPath, ["--test", ...files], { stdio: "inherit" });
process.exit(r.status ?? 1);
