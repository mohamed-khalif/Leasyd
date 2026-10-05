(() => {
  // Ingest on every record received; storage on what is kept (30 days). Telemetry per million, checks per thousand.
  // Keep in step with the table above and services/web/src/pricing.ts.
  const PRICE = {
    spans: { ingest: 0.05, storage: 0.45 }, logs: { ingest: 0.05, storage: 0.45 }, metrics: { ingest: 0.015, storage: 0.15 },
    http: { ingest: 0, storage: 0.18 }, browser: { ingest: 0, storage: 3.00 },
  };
  const NAMES = { spans: "Spans", logs: "Log records", metrics: "Metric points", http: "HTTP checks", browser: "Browser checks" };
  const PRESETS = {
    small:   { spans: 10,  logs: 20,   metrics: 50,   http: 9,   browser: 0,  drop: 0 },
    growing: { spans: 50,  logs: 100,  metrics: 200,  http: 45,  browser: 3,  drop: 20 },
    busy:    { spans: 600, logs: 1500, metrics: 2000, http: 260, browser: 30, drop: 40 },
  };
  const ids = Object.keys(PRICE);
  const el = (k) => document.getElementById("e-" + k);
  const num = (k) => Math.max(0, parseFloat(String(el(k).value).replace(/,/g, "")) || 0);
  const money = (v) => "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function update() {
    const drop = Math.min(100, num("drop")) / 100;
    let total = 0, saved = 0; const rows = [];
    for (const k of ids) {
      const n = num(k), kept = k === "spans" || k === "logs" ? n * (1 - drop) : n;
      const cost = n * PRICE[k].ingest + kept * PRICE[k].storage; total += cost;
      saved += (n - kept) * PRICE[k].storage;
      rows.push(`<div><span>${NAMES[k]}</span><span>${money(cost)}</span></div>`);
    }
    if (saved > 0) rows.push(`<div class="saved"><span>Saved by drop rules</span><span>${money(saved)}</span></div>`);
    document.getElementById("e-total").innerHTML = money(total) + " <small>/ month</small>";
    document.getElementById("e-breakdown").innerHTML = rows.join("");
  }
  [...ids, "drop"].forEach((k) => el(k).addEventListener("input", update));
  document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
    const p = PRESETS[b.dataset.preset]; [...ids, "drop"].forEach((k) => { el(k).value = p[k]; }); update();
  }));
  update();
})();
