// Runs the web app's tests (src/**/*.test.ts, *.test.tsx) with node:test, bundled by esbuild.
// Component and hook tests render into happy-dom (src/test/dom.ts); page tests answer requests
// with the mock backend (src/test/mockFetch.ts, mock/mockApi.ts). `npm test -- <filter>` runs
// only the test files whose path contains <filter>.
import { build } from "esbuild";
import { readdirSync, mkdtempSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";

const src = new URL("../src/", import.meta.url).pathname;
const filter = process.argv[2] ?? "";
const tests = readdirSync(src, { recursive: true }).map(String)
  .filter((f) => /\.test\.tsx?$/.test(f) && f.includes(filter)).map((f) => join(src, f));
const out = mkdtempSync(join(tmpdir(), "leasyd-web-tests-"));
// The mock backend finds its files next to itself (import.meta.url); keep that pointing at mock/.
const keepMockPaths = {
  name: "keep-mock-paths",
  setup(b) {
    b.onLoad({ filter: /mock[\\/]mockApi\.ts$/ }, (args) => ({
      contents: readFileSync(args.path, "utf8").replaceAll("import.meta.url", JSON.stringify(pathToFileURL(args.path).href)),
      loader: "ts",
    }));
  },
};
await build({
  entryPoints: tests, bundle: true, platform: "node", format: "esm", outdir: out, outbase: src,
  outExtension: { ".js": ".mjs" }, logLevel: "warning", jsx: "automatic", plugins: [keepMockPaths],
  define: { "import.meta.env.MODE": '"test"', "import.meta.env.DEV": "false" },
  banner: { js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);" },
});
const files = readdirSync(out, { recursive: true }).map(String).filter((f) => f.endsWith(".mjs")).map((f) => join(out, f));
const r = spawnSync(process.execPath, ["--test", "--test-concurrency=1", "--test-force-exit", ...files], { stdio: "inherit" });
process.exit(r.status ?? 1);
