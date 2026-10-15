// Prints every query of src/infra/queries.ts as JSON (with sample parameters, and builders the test
// calls itself) and the built-in Kubernetes and GPU dashboards' PromQL, for
// services/compaction/test_infra_queries.py.
import { build } from "esbuild";

const load = async (file) => {
  const out = await build({ entryPoints: [new URL(file, import.meta.url).pathname], bundle: true, write: false, format: "esm",
    platform: "node", logLevel: "silent" });
  return import("data:text/javascript;base64," + Buffer.from(out.outputFiles[0].text).toString("base64"));
};
const mod = await load("../src/infra/queries.ts");
const dash = await load("../src/builtinDashboards.ts");
const alerts = await load("../src/infra/alertTemplates.ts");
const calls = JSON.parse(process.argv[2] || "[]");   // [[name, fn, args...]]: extra builder calls
const extra = Object.fromEntries(calls.map(([name, fn, ...args]) => [name, mod[fn](...args)]));
// The built-in Kubernetes and GPU dashboards' PromQL, as "dashboards": {"<dashboard>/<panel>/<i>": {promql, type}}
const dashboards = Object.fromEntries(dash.BUILTIN.filter((d) => ["builtin-kubernetes", "builtin-gpus"].includes(d.id))
  .flatMap((d) => d.panels.flatMap((p) => (p.queries ?? []).map((q, i) => [`${d.id}/${p.id}/${i}`, { promql: q.promql, type: p.type }]))));
process.stdout.write(JSON.stringify({ ...mod.all(), ...extra, dashboards, alertTemplates: alerts.ALERT_TEMPLATES }));
