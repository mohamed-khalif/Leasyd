// Page timing on production builds (docs/phase7-report.md): requests from the browser's network log,
// no dev hooks. Per page: a fresh load (15-minute range), then one Refresh; RUNS runs per version,
// interleaved. Usage: NODE_PATH=$(npm root -g) node measure.cjs <beforeUrl> <afterUrl> <out.json> [runs]
const { chromium } = require("playwright");
const [beforeUrl, afterUrl, out, runsArg] = process.argv.slice(2);
const RUNS = Number(runsArg || 3);
const ROUTES = ["/", "/logs", "/traces", "/metrics", "/services", "/web", "/lambda", "/kubernetes", "/gpus",
  "/dashboards/builtin-services", "/query", "/sql", "/alerts", "/synthetics", "/slos", "/ai", "/start", "/drop-rules",
  "/settings", "/usage"];

function track(p) {
  const log = [], open = new Map();
  p.on("request", (r) => { const u = new URL(r.url()); if (!u.pathname.startsWith("/v1/")) return;
    const e = { method: r.method(), path: u.pathname, body: r.postData() || "", start: Date.now(), end: null }; open.set(r, e); log.push(e); });
  const done = (r) => { const e = open.get(r); if (e) { e.end = Date.now(); open.delete(r); } };
  p.on("requestfinished", done); p.on("requestfailed", done);
  return { log, pending: () => open.size, clear: () => { log.length = 0; } };
}
async function quiet(p, t) {
  let n = -1, since = Date.now(); const t0 = Date.now();
  while (Date.now() - t0 < 30_000) {
    await p.waitForTimeout(100);
    if (t.log.length !== n || t.pending()) { n = t.log.length; since = Date.now(); } else if (Date.now() - since > 1500) break;
  }
}
function stats(log, t0) {
  const q = log.filter((r) => r.method === "POST" && r.path === "/v1/app/query");
  const seen = new Map(); for (const r of log) { const k = `${r.method} ${r.path} ${r.body}`; seen.set(k, (seen.get(k) || 0) + 1); }
  const ends = new Set(q.map((r) => { try { return JSON.parse(r.body).end; } catch { return undefined; } }).filter(Boolean));
  const ev = log.flatMap((r) => [[r.start, 1], [r.end ?? r.start, -1]]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let cur = 0, peak = 0; for (const [, d] of ev) { cur += d; peak = Math.max(peak, cur); }
  const last = Math.max(t0, ...log.map((r) => r.end ?? r.start));
  return { requests: log.length, queries: q.length, dupes: [...seen.values()].reduce((a, c) => a + c - 1, 0), ends: ends.size, peak, readyMs: last - t0 };
}

async function one(browser, base, route) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await ctx.addInitScript(() => { try { localStorage.setItem("leasyd.range", "15m"); } catch { /* storage blocked: the default range */ } });
  const p = await ctx.newPage(); const t = track(p);
  const t0 = Date.now();
  await p.goto(`${base}/#${route}`); await quiet(p, t);
  const load = stats(t.log, t0);
  t.clear();
  let refresh = null;
  const btn = p.locator('button[aria-label="Refresh"]');
  if (await btn.count()) { const t1 = Date.now(); await btn.first().click(); await quiet(p, t); refresh = stats(t.log, t1); }
  await ctx.close();
  return { load, refresh };
}
async function nav(browser, base, path) {
  const ctx = await browser.newContext(); await ctx.addInitScript(() => { try { localStorage.setItem("leasyd.range", "15m"); } catch { /* storage blocked: the default range */ } });
  const p = await ctx.newPage(); const t = track(p); const t0 = Date.now();
  await p.goto(`${base}/#${path[0]}`); await quiet(p, t);
  for (const s of path.slice(1)) { await p.evaluate((h) => { location.hash = h; }, s); await quiet(p, t); }
  const s = stats(t.log, t0); await ctx.close(); return s;
}

(async () => {
  const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const res = { before: {}, after: {}, navBefore: {}, navAfter: {} };
  for (let run = 0; run < RUNS; run++) {
    for (const route of ROUTES) {
      for (const [side, base] of run % 2 ? [["after", afterUrl], ["before", beforeUrl]] : [["before", beforeUrl], ["after", afterUrl]]) {
        (res[side][route] ??= []).push(await one(browser, base, route));
      }
    }
    console.log("run", run + 1, "done");
  }
  for (const [name, path] of [["Home → Traces → Home", ["/", "/traces", "/"]], ["Kubernetes → GPUs → Kubernetes → GPUs", ["/kubernetes", "/gpus", "/kubernetes", "/gpus"]]]) {
    res.navBefore[name] = await nav(browser, beforeUrl, path); res.navAfter[name] = await nav(browser, afterUrl, path);
  }
  require("fs").writeFileSync(out, JSON.stringify(res));
  await browser.close();
})();
