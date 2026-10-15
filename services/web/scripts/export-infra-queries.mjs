// Prints every query of src/infra/queries.ts as JSON (with sample parameters, and builders the test
// calls itself), for services/compaction/test_infra_queries.py.
import { build } from "esbuild";

const out = await build({ entryPoints: [new URL("../src/infra/queries.ts", import.meta.url).pathname], bundle: true,
  write: false, format: "esm", platform: "node", logLevel: "silent" });
const mod = await import("data:text/javascript;base64," + Buffer.from(out.outputFiles[0].text).toString("base64"));
const calls = JSON.parse(process.argv[2] || "[]");   // [[name, fn, args...]]: extra builder calls
const extra = Object.fromEntries(calls.map(([name, fn, ...args]) => [name, mod[fn](...args)]));
process.stdout.write(JSON.stringify({ ...mod.all(), ...extra }));
