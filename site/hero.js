// The landing page's animated dashboard: a tilted Leasyd dashboard whose panels keep changing
// (stat tiles, service health hexagons, requests, log volume, span durations, errors by service,
// top errors), with the AI SRE's finding popping up now and then. Made of plain DOM and SVG; no
// data is fetched. Stops moving for people who ask for reduced motion.
(function () {
  const board = document.getElementById("board");
  if (!board) return;
  const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const NS = "http://www.w3.org/2000/svg";
  const rnd = (a, b) => a + Math.random() * (b - a);
  const pick = (xs) => xs[Math.floor(Math.random() * xs.length)];
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const svg = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
  const panel = (cls, title) => { const p = el("div", "bp " + cls); p.appendChild(el("div", "bp-t", title)); board.appendChild(p); return p; };

  board.textContent = "";
  const side = el("div", "b-side");
  for (let i = 0; i < 11; i++) side.appendChild(el("i"));
  board.appendChild(side);

  // -- stat tiles
  const stat = (cls, title, unit) => {
    const p = panel("b-stat " + cls, title);
    const v = el("div", "b-num"); p.appendChild(v);
    return { p, v, unit, value: 0 };
  };
  const p95 = stat("s1", "p95 checkout latency", "ms");
  const err = stat("s2", "Error rate", "%");

  // -- service health hexagons, two layouts that swap (all services; grouped by team)
  const hexP = panel("b-hex", "Service health · 24 services");
  const hexSvg = svg("svg", { viewBox: "20 8 380 200", class: "hexes" });
  hexP.appendChild(hexSvg);
  const hexPath = (cx, cy, r) => {
    let d = "";
    for (let i = 0; i < 6; i++) {
      const a = Math.PI / 180 * (60 * i - 30);
      d += (i ? "L" : "M") + (cx + r * Math.cos(a)).toFixed(1) + "," + (cy + r * Math.sin(a)).toFixed(1);
    }
    return d + "Z";
  };
  const layouts = [[], []];
  const R = 19, W = R * Math.sqrt(3);
  for (let row = 0; row < 5; row++) for (let col = 0; col < (row % 2 ? 6 : 7); col++) {      // one honeycomb
    if (layouts[0].length < 24) layouts[0].push([95 + col * W + (row % 2 ? W / 2 : 0), 50 + row * R * 1.5]);
  }
  [[60, 40], [190, 30], [320, 45], [120, 130], [265, 125]].forEach(([gx, gy], g) => {        // five teams
    const n = [6, 5, 4, 5, 4][g];
    const spots = [[0, 0], [W, 0], [W / 2, R * 1.5], [-W / 2, R * 1.5], [W * 1.5, R * 1.5], [0, R * 3]];
    for (let i = 0; i < n; i++) layouts[1].push([gx + spots[i][0], gy + spots[i][1]]);
  });
  const hexes = layouts[0].map(() => { const h = svg("path", { class: "hx ok" }); hexSvg.appendChild(h); return h; });
  let layout = 0;
  const placeHexes = () => hexes.forEach((h, i) => { const [x, y] = layouts[layout][i]; h.setAttribute("d", hexPath(x, y, R - 1.5)); });
  placeHexes();

  // -- requests per second: an area chart that scrolls
  const reqP = panel("b-req", "Requests / s");
  const reqSvg = svg("svg", { viewBox: "0 0 200 90", preserveAspectRatio: "none" });
  const reqArea = svg("path", { class: "area" }), reqLine = svg("path", { class: "line" });
  reqSvg.append(reqArea, reqLine); reqP.appendChild(reqSvg);
  const req = Array.from({ length: 40 }, (_, i) => 45 + 18 * Math.sin(i / 4) + rnd(-6, 6));
  const drawReq = () => {
    const pts = req.map((v, i) => [i * 200 / 39, 90 - v]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join("");
    reqLine.setAttribute("d", line);
    reqArea.setAttribute("d", line + "L200,90L0,90Z");
  };

  // -- top errors table
  const topP = panel("b-top", "Top errors");
  const msgs = ["payment declined: insufficient funds", "timeout calling pricing-api", "cart not found", "connection reset by peer",
                "rate limited by stripe", "image resize failed", "deadline exceeded", "null order id"];
  const rows = Array.from({ length: 6 }, () => { const r = el("div", "b-row"); r.append(el("span", "sev"), el("span", "msg"), el("b")); topP.appendChild(r); return r; });

  // -- log volume by severity: stacked bars
  const logP = panel("b-logs", "Log volume by severity");
  const logBox = el("div", "bars"); logP.appendChild(logBox);
  const logBars = Array.from({ length: 36 }, () => {
    const b = el("div", "bar"); ["e", "w", "i"].forEach((c) => b.appendChild(el("i", c))); logBox.appendChild(b); return b;
  });

  // -- span durations: a purple histogram with an outlier tail
  const spanP = panel("b-spans", "Span duration · outliers");
  const spanBox = el("div", "bars thin"); spanP.appendChild(spanBox);
  const spanBars = Array.from({ length: 30 }, () => { const b = el("div", "bar"); b.appendChild(el("i", "p")); spanBox.appendChild(b); return b; });

  // -- errors by service: a donut
  const donP = panel("b-donut", "Errors by service");
  const donSvg = svg("svg", { viewBox: "0 0 120 120" });
  const colors = ["#e5484d", "#f5a524", "#8e4ec6", "#3e9bf5", "#30a46c"];
  const arcs = colors.map((c) => { const a = svg("circle", { cx: 60, cy: 60, r: 42, fill: "none", stroke: c, "stroke-width": 16, class: "arc" }); donSvg.appendChild(a); return a; });
  const donNum = svg("text", { x: 60, y: 66, "text-anchor": "middle", class: "dnum" });
  donSvg.appendChild(donNum); donP.appendChild(donSvg);
  const legend = el("div", "legend");
  ["checkout", "payment", "pricing-api", "cart", "frontend"].forEach((s, i) => legend.appendChild(el("span", "", `<i style="background:${colors[i]}"></i>${s}`)));
  donP.appendChild(legend);

  // -- the AI SRE's finding
  const pop = el("div", "b-pop", '<b><span class="spark">✦</span> Leasyd AI</b><p>Checkout errors started at <code>14:02</code>: <code>pricing-api</code> calls time out after the 14:00 deploy.</p><span class="pop-link">View trace →</span>');
  board.appendChild(pop);

  // -- one step of "live" data
  let t = 0, incident = false;
  function step() {
    t++;
    if (t % 6 === 1) incident = !incident;          // an incident comes and goes
    p95.value = incident ? rnd(380, 520) : rnd(110, 190);
    err.value = incident ? rnd(1.6, 3.2) : rnd(0.05, 0.3);
    for (const s of [p95, err]) {
      s.v.innerHTML = (s.unit === "%" ? s.value.toFixed(2) : Math.round(s.value)) + "<small>" + s.unit + "</small>";
    }
    p95.p.dataset.level = p95.value > 300 ? "bad" : p95.value > 160 ? "warn" : "good";
    err.p.dataset.level = err.value > 1 ? "bad" : err.value > 0.2 ? "warn" : "good";

    if (t % 3 === 0) { layout = 1 - layout; placeHexes(); }
    hexes.forEach((h, i) => {
      const r = Math.random();
      h.setAttribute("class", "hx " + (incident && i % 5 === 2 ? (r < 0.6 ? "bad" : "warn") : r < 0.08 ? "warn" : r < 0.12 ? "idle" : "ok"));
    });

    req.shift(); req.push(Math.max(8, Math.min(85, req[req.length - 1] + rnd(-9, 9) + (incident ? -4 : 2))));
    drawReq();

    rows.forEach((r, i) => {
      r.className = "b-row" + (incident && i < 2 ? " hot" : "");
      r.children[0].className = "sev " + (i < 2 || Math.random() < 0.3 ? "e" : "w");
      r.children[1].textContent = i === 0 && incident ? "timeout calling pricing-api" : pick(msgs);
      r.children[2].textContent = Math.round(incident && i < 2 ? rnd(800, 2400) : rnd(4, 300));
    });

    logBars.forEach((b, i) => {
      const hot = incident && i > 24;
      const [e, w, inf] = b.children;
      e.style.height = (hot ? rnd(18, 40) : rnd(0, 6)) + "%";
      w.style.height = rnd(4, 14) + "%";
      inf.style.height = rnd(20, 45) + "%";
    });
    spanBars.forEach((b, i) => {
      const base = Math.exp(-Math.pow((i - 8) / 5, 2)) * 85 + 6;
      b.firstChild.style.height = Math.min(96, base + rnd(-6, 6) + (incident && i > 20 ? rnd(10, 30) : 0)) + "%";
    });

    const shares = colors.map((_, i) => (i === 0 && incident ? 5 : 1) * rnd(0.5, 1.5));
    const total = shares.reduce((a, b) => a + b, 0), C = 2 * Math.PI * 42;
    let off = 0;
    arcs.forEach((a, i) => {
      const len = (shares[i] / total) * C;
      a.setAttribute("stroke-dasharray", `${Math.max(0, len - 2)} ${C}`);
      a.setAttribute("stroke-dashoffset", String(-off));
      off += len;
    });
    donNum.textContent = Math.round(incident ? rnd(2400, 3600) : rnd(120, 400));

    pop.classList.toggle("on", incident && t % 6 >= 3);
  }

  step();
  if (!still) setInterval(step, 1600);
})();
