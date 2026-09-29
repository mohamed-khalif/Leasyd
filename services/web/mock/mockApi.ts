// A fake /v1/app backend for `npm run mock`: realistic-looking answers to the
// same queries the UI sends, so the UI can be built and screenshotted
// without AWS. Deterministic (seeded) so screenshots are stable.
import type { Plugin } from "vite";

const SERVICES = ["frontend-proxy", "frontend", "checkout", "cart", "product-catalog", "payment", "shipping",
  "recommendation", "currency", "email", "ad", "quote", "fraud-detection", "accounting", "load-generator"];
const WEIGHT = SERVICES.map((_, i) => 1 / (i + 1));
const OPS: Record<string, string[]> = {
  "frontend-proxy": ["ingress", "GET /api/products", "GET /images/products/:filename"],
  frontend: ["GET /", "GET /api/cart", "POST /api/checkout", "GET /_next/static/chunks/:filename"],
  checkout: ["oteldemo.CheckoutService/PlaceOrder", "prepareOrderItemsAndShippingQuoteFromCart"],
  cart: ["oteldemo.CartService/GetCart", "oteldemo.CartService/AddItem", "HGET", "HMSET"],
  "product-catalog": ["oteldemo.ProductCatalogService/GetProduct", "oteldemo.ProductCatalogService/ListProducts"],
  payment: ["oteldemo.PaymentService/Charge", "charge"],
  shipping: ["oteldemo.ShippingService/GetQuote", "oteldemo.ShippingService/ShipOrder"],
};
const METRICS = ["http.server.request.duration", "rpc.server.duration", "process.runtime.memory", "db.client.connections.usage",
  "kafka.consumer.lag", "system.cpu.utilization", "jvm.gc.duration", "http.client.request.duration"];
const MESSAGES = [
  ["INFO", 9, "request completed"], ["INFO", 9, "cart updated for user"], ["INFO", 9, "order placed successfully"],
  ["DEBUG", 5, "cache hit for product catalog"], ["DEBUG", 5, "resolved 12 recommendations"],
  ["WARN", 13, "slow response from downstream: 1480ms"], ["WARN", 13, "retrying request (attempt 2 of 3)"],
  ["ERROR", 17, "payment declined: card expired"], ["ERROR", 17, "timeout calling shipping quote service"],
] as const;

function rng(seed: number) { return () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32); }
const hex = (r: () => number, n: number) => Array.from({ length: n }, () => Math.floor(r() * 16).toString(16)).join("");

type Q = { signal: string; start: string; end: string; group_by?: string[]; aggs?: { fn: string; field?: string }[];
  search?: { limit: number }; where?: { field: string; op: string; value?: unknown }[]; services?: string[];
  match?: Record<string, string>; limit?: number };

const PER_MIN = { logs: 5200, traces: 9100, metrics: 14000 } as Record<string, number>;

function answer(q: Q) {
  const t0 = Date.parse(q.start), t1 = Date.parse(q.end), mins = Math.max(1, (t1 - t0) / 60000);
  const r = rng(Math.floor(t1 / 60000) % 997 + q.signal.length);
  const minSev = Number(q.where?.find((w) => w.field === "severity_number")?.value ?? 0);
  const text = String(q.where?.find((w) => w.field === "body")?.value ?? "").toLowerCase();
  const sevShare: Record<string, number> = { INFO: 0.72, DEBUG: 0.14, WARN: 0.09, ERROR: 0.05 };
  const sevOk = (s: string) => ({ DEBUG: 5, INFO: 9, WARN: 13, ERROR: 17 } as Record<string, number>)[s] >= minSev;
  const svcs = q.services?.length ? q.services : SERVICES;
  const svcShare = (s: string) => WEIGHT[SERVICES.indexOf(s)] / WEIGHT.reduce((a, b) => a + b, 0);
  const total = PER_MIN[q.signal] * mins * (q.services?.length ? svcs.reduce((a, s) => a + svcShare(s), 0) : 1)
    * (q.signal === "logs" ? Object.keys(sevShare).filter(sevOk).reduce((a, s) => a + sevShare[s], 0) : 1)
    * (text ? 0.04 : 1);

  if (q.search) return search(q, r, t0, t1, svcs, sevOk, text);

  const by = q.group_by ?? [];
  const aggs = q.aggs ?? [{ fn: "count" }];
  const cols = [...by, ...aggs.map((a) => (a.fn === "count" ? "count" : `${a.fn}(${a.field})`))];
  const dims = by.map((g) => values(g, q, t0, t1, svcs, sevOk));
  let combos: { key: unknown[]; share: number }[] = [{ key: [], share: 1 }];
  for (const d of dims) combos = combos.flatMap((c) => d.map((v) => ({ key: [...c.key, v.value], share: c.share * v.share })));
  const rows = combos.map((c) => [...c.key, ...aggs.map((a) => {
    if (a.fn === "count") return Math.round(total * c.share * (0.85 + r() * 0.3));
    const base = 18e6 * (1 + (SERVICES.indexOf(String(c.key[0])) % 5)) * (0.7 + r() * 0.6);
    return base * (({ p50: 1, p90: 2.6, p95: 3.4, p99: 6.8 } as Record<string, number>)[a.fn] ?? 1);
  })]).filter((row) => Number(row[by.length]) > 0);
  if (!by.some((g) => g.startsWith("ts:"))) rows.sort((a, b) => Number(b[by.length]) - Number(a[by.length]));
  return { columns: cols, rows: rows.slice(0, q.limit ?? 100) };
}

function values(g: string, _q: Q, t0: number, t1: number, svcs: string[], sevOk: (s: string) => boolean) {
  if (g.startsWith("ts:")) {
    const b = Number(g.slice(3)) * 1000, out = [];
    const n = Math.floor((t1 - t0) / b);
    for (let i = 0; i <= n; i++) {
      const t = Math.floor(t0 / b) * b + i * b;
      const wave = 1 + 0.25 * Math.sin(t / 900000) + 0.12 * Math.sin(t / 97000) + (i % 17 === 5 ? 0.5 : 0);
      out.push({ value: new Date(t).toISOString().replace("Z", "000Z"), share: (wave / (n + 1)) });
    }
    return out;
  }
  if (g === "service") {
    const w = svcs.map((s) => WEIGHT[SERVICES.indexOf(s)]), sum = w.reduce((a, b) => a + b, 0);
    return svcs.map((s, i) => ({ value: s, share: w[i] / sum }));
  }
  if (g === "severity_text") return [["INFO", .72], ["DEBUG", .14], ["WARN", .09], ["ERROR", .05]].filter(([s]) => sevOk(String(s))).map(([v, s]) => ({ value: v, share: Number(s) }));
  if (g === "kind") return [["SPAN_KIND_SERVER", .46], ["SPAN_KIND_CLIENT", .38], ["SPAN_KIND_INTERNAL", .12], ["SPAN_KIND_PRODUCER", .04]].map(([v, s]) => ({ value: v, share: Number(s) }));
  if (g === "name") return Object.values(OPS).flat().map((v, i) => ({ value: v, share: 1 / (i + 2) / 3 }));
  if (g === "metric_name") return METRICS.map((v, i) => ({ value: v, share: 1 / (i + 1.5) / 2.2 }));
  return [{ value: null, share: 1 }];
}

function search(q: Q, r: () => number, t0: number, t1: number, svcs: string[], sevOk: (s: string) => boolean, text: string) {
  const limit = q.search!.limit;
  if (q.signal === "traces") {
    const trace = q.match?.trace_id ?? hex(r, 32);
    const spans = q.match?.trace_id ? traceSpans(trace, r, t1 - 120000) : Array.from({ length: limit }, (_, i) => ({
      ...span(r, t1 - i * 7000, SERVICES[Math.floor(r() ** 2 * 7)], hex(r, 32), hex(r, 16), null, 2e6 + r() ** 3 * 2.4e9),
    }));
    const cols = Object.keys(spans[0]);
    return { columns: cols, rows: spans.map((s) => cols.map((c) => (s as Record<string, unknown>)[c])) };
  }
  const rows = [];
  const n = q.match?.trace_id ? 7 : limit;
  for (let i = 0; i < n * 3 && rows.length < n; i++) {
    const [sev, num, body] = MESSAGES[Math.floor(r() * MESSAGES.length)];
    if (!sevOk(sev) || (text && !body.includes(text))) continue;
    const svc = svcs[Math.floor(r() ** 2 * svcs.length)];
    const ts = t1 - (q.match ? 118000 - i * 3000 : i * ((t1 - t0) / n) * r());
    rows.push({
      ts: new Date(ts).toISOString(), ts_unix_nano: ts * 1e6, service: svc, severity_number: num, severity_text: sev,
      body: `${body} order_id=${hex(r, 8)}`, trace_id: q.match?.trace_id ?? hex(r, 32), span_id: hex(r, 16), scope_name: `${svc}-logger`,
      attributes: { "http.route": "/api/cart", "user.id": `u-${hex(r, 6)}`, "request.id": hex(r, 12) },
      resource_attributes: { "service.name": svc, "host.name": `ip-10-1-${Math.floor(r() * 40)}-${Math.floor(r() * 250)}`, "k8s.namespace.name": "shop" },
    });
  }
  const cols = rows.length ? Object.keys(rows[0]) : ["ts"];
  return { columns: cols, rows: rows.map((x) => cols.map((c) => (x as Record<string, unknown>)[c])) };
}

function span(r: () => number, start: number, svc: string, trace: string, id: string, parent: string | null, dur: number, name?: string) {
  const ops = OPS[svc] ?? ["handle"];
  return {
    ts: new Date(start).toISOString(), ts_unix_nano: start * 1e6, end_ts: new Date(start + dur / 1e6).toISOString(), duration_ns: Math.round(dur),
    service: svc, name: name ?? ops[Math.floor(r() * ops.length)], kind: parent ? "SPAN_KIND_CLIENT" : "SPAN_KIND_SERVER",
    status_code: r() < 0.06 ? "STATUS_CODE_ERROR" : "STATUS_CODE_UNSET", trace_id: trace, span_id: id, parent_span_id: parent,
    attributes: { "http.method": "POST", "rpc.system": "grpc" }, resource_attributes: { "service.name": svc },
  };
}

function traceSpans(trace: string, r: () => number, t: number) {
  const out: ReturnType<typeof span>[] = [];
  const root = span(r, t, "frontend-proxy", trace, hex(r, 16), null, 412e6, "ingress POST /api/checkout");
  out.push(root);
  const fe = span(r, t + 1, "frontend", trace, hex(r, 16), root.span_id, 405e6, "POST /api/checkout");
  out.push(fe);
  const co = span(r, t + 4, "checkout", trace, hex(r, 16), fe.span_id, 380e6, "oteldemo.CheckoutService/PlaceOrder");
  out.push(co);
  let at = t + 8;
  for (const [svc, name, d] of [["cart", "oteldemo.CartService/GetCart", 22e6], ["product-catalog", "oteldemo.ProductCatalogService/GetProduct", 9e6],
    ["currency", "Convert", 4e6], ["shipping", "oteldemo.ShippingService/GetQuote", 61e6], ["payment", "oteldemo.PaymentService/Charge", 142e6],
    ["email", "SendOrderConfirmation", 48e6], ["accounting", "process order", 18e6]] as [string, string, number][]) {
    const s = span(r, at, svc, trace, hex(r, 16), co.span_id, d, name);
    out.push(s);
    if (svc === "cart") out.push(span(r, at + 2, "cart", trace, hex(r, 16), s.span_id, 6e6, "HGET"));
    if (svc === "payment") out.push(span(r, at + 30, "payment", trace, hex(r, 16), s.span_id, 96e6, "charge"));
    at += d / 1e6 + 3;
  }
  return out;
}

export function mockApi(): Plugin {
  return {
    name: "leasyd-mock-api",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url?.startsWith("/v1/app/")) return next();
        res.setHeader("Content-Type", "application/json");
        if (req.url === "/v1/app/me") return res.end(JSON.stringify({ tenant: "acme", email: "ana@acme.io" }));
        let body = "";
        req.on("data", (c: Buffer) => (body += c));
        req.on("end", () => {
          const out = answer(JSON.parse(body || "{}"));
          setTimeout(() => res.end(JSON.stringify(out)), 150 + Math.random() * 250);
        });
      });
    },
  };
}
