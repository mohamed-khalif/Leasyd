// The queries each page sends on a fresh load, per time range, as the browser sends them: the built
// app on the mock backend (`npx vite --mode mock`), every POST /v1/app/query body in order. For
// infra/bench/bench.py, which replays them on the real engine for a real account.
// Usage: NODE_PATH=$(npm root -g) node capture-queries.cjs http://localhost:5199 out.json
const { chromium } = require("playwright");
const fs = require("fs");
const [base, out] = process.argv.slice(2);
const ROUTES = ["/", "/logs", "/traces", "/metrics", "/services", "/web", "/agents", "/lambda", "/kubernetes", "/gpus",
  "/dashboards/builtin-services", "/alerts", "/synthetics", "/slos"];
const RANGES = ["1h", "6h", "24h"];

(async () => {
  const browser = await chromium.launch();
  const pages = [];
  for (const range of RANGES) {
    for (const route of ROUTES) {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      await ctx.addInitScript((r) => { try { localStorage.setItem("leasyd.range", r); } catch { /* default range */ } }, range);
      const p = await ctx.newPage();
      const queries = [];
      p.on("request", (r) => {
        const u = new URL(r.url());
        if (r.method() === "POST" && u.pathname === "/v1/app/query") queries.push({ at: Date.now(), body: JSON.parse(r.postData() || "{}") });
      });
      const t0 = Date.now();
      await p.goto(`${base}/#${route}`);
      let n = -1, since = Date.now();
      while (Date.now() - t0 < 30_000) {            // settled: no new query for 3 s
        await p.waitForTimeout(250);
        if (queries.length !== n) { n = queries.length; since = Date.now(); } else if (Date.now() - since > 3000) break;
      }
      pages.push({ route, range, queries: queries.map((q) => ({ offsetMs: q.at - t0, ...q.body })) });
      console.log(range, route, queries.length, "queries");
      await ctx.close();
    }
  }
  await browser.close();
  fs.writeFileSync(out, JSON.stringify({ captured: new Date().toISOString(), pages }, null, 1));
})();
