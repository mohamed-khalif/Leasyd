(() => {
  const PRICE = { spans: 0.30, logs: 0.30, metrics: 0.10, http: 0.20, browser: 3.00 };   // per million (telemetry) / thousand (checks)
  const NAMES = { spans: "Spans", logs: "Log records", metrics: "Metric points", http: "HTTP checks", browser: "Browser checks" };
  const PRESETS = {
    small:   { spans: 10,  logs: 20,   metrics: 50,   http: 9,   browser: 0 },
    growing: { spans: 50,  logs: 100,  metrics: 200,  http: 45,  browser: 3 },
    busy:    { spans: 600, logs: 1500, metrics: 2000, http: 260, browser: 30 },
  };
  const ids = Object.keys(PRICE);
  const el = (k) => document.getElementById("e-" + k);
  const money = (v) => "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function update() {
    let total = 0; const rows = [];
    for (const k of ids) {
      const n = Math.max(0, parseFloat(String(el(k).value).replace(/,/g, "")) || 0);
      const cost = n * PRICE[k]; total += cost;
      rows.push(`<div><span>${NAMES[k]}</span><span>${money(cost)}</span></div>`);
    }
    document.getElementById("e-total").innerHTML = money(total) + " <small>/ month</small>";
    document.getElementById("e-breakdown").innerHTML = rows.join("");
  }
  ids.forEach((k) => el(k).addEventListener("input", update));
  document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
    const p = PRESETS[b.dataset.preset]; ids.forEach((k) => { el(k).value = p[k]; }); update();
  }));
  update();
})();
