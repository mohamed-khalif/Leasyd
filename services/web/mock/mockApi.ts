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

// ------------------------------------------------------------------ synthetic checks

type MockCheck = Record<string, unknown> & { id: string; name: string; frequency: number; enabled: boolean };
const step = (name: string, method: string, url: string, extra: Record<string, unknown> = {}) => ({ name, method, url, headers: {}, auth: { type: "none" },
  follow_redirects: true, verify_tls: true, record_body: true, extract: [], constraints: [{ type: "status", expr: "<400" }], ...extra });
const CHECKS: MockCheck[] = [
  ["a1b2c3d4e5f6", "Homepage", [step("Load homepage", "GET", "https://shop.example.com/", { constraints: [{ type: "status", expr: "2xx" }, { type: "body_contains", value: "Add to cart" }, { type: "tls_days", value: 14 }] })], 1, 0.9993, 180],
  ["b2c3d4e5f6a1", "Checkout API", [
    step("Log in", "POST", "{base}/v1/login", { auth: { type: "basic", username: "monitor@shop.example.com", password: "{password}" },
      extract: [{ name: "token", from: "json", expr: "access_token" }], constraints: [{ type: "status", expr: "200" }] }),
    step("Create cart", "POST", "{base}/v1/carts", { auth: { type: "bearer", token: "{token}" }, body: '{"sku": "OLJCESPC7Z", "qty": 1}',
      extract: [{ name: "cart_id", from: "json", expr: "cart.id" }], constraints: [{ type: "status", expr: "201" }, { type: "max_ms", value: 800 }] }),
    step("Get cart", "GET", "{base}/v1/carts/{cart_id}", { auth: { type: "bearer", token: "{token}" },
      constraints: [{ type: "status", expr: "2xx" }, { type: "json", path: "cart.items[0].sku", op: "equals", value: "OLJCESPC7Z" }] }),
  ], 1, 0.981, 95],
  ["c3d4e5f6a1b2", "Product search", [step("Search", "GET", "https://api.shop.example.com/v1/search?q=shoes", { constraints: [{ type: "json", path: "results", op: "exists" }] })], 5, 1, 320],
  ["d4e5f6a1b2c3", "Status page", [step("Status", "GET", "https://status.shop.example.com/")], 15, 1, 140],
].map(([id, name, steps, frequency, up, ms]) => ({ id: id as string, name: name as string, steps, frequency: frequency as number,
  timeout_ms: 20000, variables: id === "b2c3d4e5f6a1" ? { base: "https://api.shop.example.com" } : {}, enabled: id !== "d4e5f6a1b2c3",
  secret_names: id === "b2c3d4e5f6a1" ? ["password"] : [], created_at: "2026-09-20T10:00:00Z", updated_at: "2026-09-20T10:00:00Z", up, ms }));

function synthetics(q: Q) {
  const t0 = Date.parse(q.start), t1 = Date.parse(q.end);
  const metric = String(q.where?.find((w) => w.field === "metric_name")?.value ?? "");
  const only = q.where?.find((w) => w.field === "attributes.check.id")?.value;
  const cs = CHECKS.filter((c) => c.enabled && (!only || c.id === only));
  const r = rng(Math.floor(t1 / 60000));
  const fail = (c: MockCheck, t: number) => Number(c.up) < 1 && Math.sin(t / 7e5 + c.id.charCodeAt(0)) > 0.93;
  if (q.search) {
    const rows = [];
    for (const c of cs) for (let t = t1 - 30000; t > t0 && rows.length < q.search.limit; t -= c.frequency * 60000) {
      const bad = fail(c, t), ms = Number(c.ms) * (0.8 + r() * 0.5) * (bad ? 6 : 1);
      rows.push({ ts: new Date(t).toISOString(), service: "synthetics", name: c.name, trace_id: hex(r, 32), span_id: hex(r, 16),
        duration_ns: Math.round(ms * 1e6), status_code: bad ? 2 : 0,
        attributes: { "check.id": c.id, "check.name": c.name, "check.result": bad ? "fail" : "pass", "check.total_ms": Math.round(ms),
          ...(bad ? { "check.failed_step": 2, "check.failure": "Create cart: status 503, expected 201" } : {}) } });
    }
    const cols = rows.length ? Object.keys(rows[0]) : ["ts"];
    return { columns: cols, rows: rows.map((x) => cols.map((k) => (x as Record<string, unknown>)[k])) };
  }
  const by = q.group_by ?? [], aggs = q.aggs ?? [{ fn: "count" }];
  const tsg = by.find((g) => g.startsWith("ts:")), b = tsg ? Number(tsg.slice(3)) * 1000 : t1 - t0;
  const rows: unknown[][] = [];
  for (const c of cs) for (let t = Math.floor(t0 / b) * b; t <= t1; t += b) {
    const up = Number(c.up) + (tsg && fail(c, t) ? -0.3 : 0), ms = Number(c.ms) * (0.9 + 0.2 * Math.sin(t / 3e6 + c.frequency));
    const v = (a: { fn: string }) => a.fn === "count" ? Math.round((b / 60000) / c.frequency)
      : metric.endsWith("success") ? up : metric.endsWith("tls_days_remaining") ? 58 + c.frequency : a.fn === "p95" ? ms * 1.6 : ms;
    if (by.includes("attributes.step.index")) {
      (c.steps as unknown[]).forEach((_, i) => rows.push([i + 1, ...aggs.map((a) => (a.fn === "p95" ? 1.6 : 1) * Number(c.ms) / (c.steps as unknown[]).length * (1 + i * 0.3))]));
      break;
    }
    rows.push([...by.map((g) => g.startsWith("ts:") ? new Date(t).toISOString().replace("Z", "000Z") : c.id), ...aggs.map(v)]);
    if (!tsg) break;
  }
  return { columns: [...by, ...aggs.map((a) => (a.fn === "count" ? "count" : `${a.fn}(${a.field})`))], rows };
}

function answer(q: Q) {
  if (q.services?.includes("synthetics")) return synthetics(q);
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

  if (q.signal === "metrics") return metrics(q, t0, t1, svcs);
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
  return [{ value: null, share: 1 }];
}

// ------------------------------------------------------------------ metrics

type Def = { name: string; type: string; temporality: number | null; mono: boolean | null; unit: string; base: number;
  svcs: string[]; description: string };
const DEFS: Def[] = [
  { name: "http.server.request.duration", type: "histogram", temporality: 2, mono: null, unit: "ms", base: 38,
    svcs: ["frontend", "checkout", "cart", "product-catalog"], description: "Duration of HTTP server requests" },
  { name: "http.server.requests", type: "sum", temporality: 2, mono: true, unit: "{request}", base: 42,
    svcs: ["frontend", "checkout", "cart", "product-catalog"], description: "HTTP requests handled" },
  { name: "rpc.server.duration", type: "histogram", temporality: 1, mono: null, unit: "ms", base: 12,
    svcs: ["checkout", "payment", "shipping", "currency"], description: "Duration of inbound RPCs" },
  { name: "system.cpu.utilization", type: "gauge", temporality: null, mono: null, unit: "1", base: 0.42,
    svcs: SERVICES.slice(0, 8), description: "CPU in use, 0-1" },
  { name: "process.runtime.memory", type: "sum", temporality: 2, mono: false, unit: "MiBy", base: 310,
    svcs: SERVICES.slice(0, 8), description: "Memory in use by the runtime" },
  { name: "kafka.consumer.lag", type: "gauge", temporality: null, mono: null, unit: "{message}", base: 1200,
    svcs: ["accounting", "fraud-detection"], description: "Messages behind the head of the partition" },
  { name: "db.client.connections.usage", type: "sum", temporality: 2, mono: false, unit: "{connection}", base: 14,
    svcs: ["cart", "product-catalog", "accounting"], description: "Connections in use" },
  { name: "jvm.gc.duration", type: "histogram", temporality: 1, mono: null, unit: "s", base: 0.018,
    svcs: ["ad", "fraud-detection"], description: "Time spent in garbage collection" },
];
const ATTRS: Record<string, string[]> = {
  "attributes.http.route": ["/api/cart", "/api/checkout", "/api/products", "/api/recommendations"],
  "attributes.http.request.method": ["GET", "POST"],
  "resource.host.name": ["ip-10-1-4-17", "ip-10-1-9-201", "ip-10-1-22-8"],
};

function metrics(q: Q, t0: number, t1: number, svcs: string[]) {
  const name = q.where?.find((w) => w.field === "metric_name")?.value;
  const defs = DEFS.filter((d) => !name || d.name === name);
  if (q.search) {
    const rows = defs.flatMap((d) => d.svcs.filter((s) => svcs.includes(s)).slice(0, 3).map((svc, i) => ({
      ts: new Date(t1 - i * 10000).toISOString(), service: svc, metric_name: d.name, metric_type: d.type, unit: d.unit,
      description: d.description, value: d.base, attributes: { "http.route": ATTRS["attributes.http.route"][i], "http.request.method": "GET" },
      resource_attributes: { "service.name": svc, "host.name": ATTRS["resource.host.name"][i % 3] },
    }))).slice(0, q.search!.limit);
    const cols = rows.length ? Object.keys(rows[0]) : ["ts"];
    return { columns: cols, rows: rows.map((x) => cols.map((c) => (x as Record<string, unknown>)[c])) };
  }
  const by = q.group_by ?? [], aggs = q.aggs ?? [{ fn: "count" }];
  const tsg = by.find((g) => g.startsWith("ts:")), b = tsg ? Number(tsg.slice(3)) : (t1 - t0) / 1000;
  const times = tsg ? Array.from({ length: Math.floor((t1 - t0) / 1000 / b) + 1 }, (_, i) => (Math.floor(t0 / 1000 / b) + i) * b * 1000) : [t0];
  const attrKey = by.find((g) => ATTRS[g]);
  const groups = new Map<string, { key: unknown[]; vals: number[][] }>();
  for (const d of defs) for (const svc of d.svcs.filter((s) => svcs.includes(s))) for (const t of times)
    for (const [ai, av] of (attrKey ? ATTRS[attrKey] : [null]).entries()) {
      const h = (SERVICES.indexOf(svc) + 1) * (ai + 1.7);
      const wave = 1 + 0.18 * Math.sin(t / 1.8e6 + h) + 0.07 * Math.sin(t / 2.3e5 + h * 3);
      const level = d.base * (0.6 + (h % 1.3)) * wave, share = attrKey ? 1 / ATTRS[attrKey].length : 1;
      const vals = aggs.map((a) => {
        if (a.fn === "count") return Math.round((b / 10) * share);
        if (a.fn === "increase" && a.field === "count") return level * 0.9 * b * share;          // histogram: observations
        if (a.fn === "increase" && a.field === "sum") return level * 0.9 * b * share * d.base * wave;
        if (a.fn === "increase") return level * b * share;                                         // counter
        if (a.field === "sum") return d.base * 40;
        if (a.field === "count") return 40;
        return a.fn === "max" ? level * 1.25 : a.fn === "min" ? level * 0.8 : a.fn === "p95" ? level * 1.15 : level;
      });
      const key = by.map((g) => g === "metric_name" ? d.name : g === "metric_type" ? d.type : g === "temporality" ? d.temporality
        : g === "is_monotonic" ? d.mono : g === "unit" ? d.unit : g === "service" ? svc
        : g.startsWith("ts:") ? new Date(t).toISOString().replace("Z", "000Z") : g === attrKey ? av : null);
      const k = JSON.stringify(key);
      if (!groups.has(k)) groups.set(k, { key, vals: [] });
      groups.get(k)!.vals.push(vals);
    }
  const rows = [...groups.values()].map(({ key, vals }) => [...key, ...aggs.map((a, i) => {
    const xs = vals.map((v) => v[i]);
    if (a.fn === "count" || a.fn === "increase") return xs.reduce((x, y) => x + y, 0);
    if (a.fn === "max") return Math.max(...xs);
    if (a.fn === "min") return Math.min(...xs);
    return xs.reduce((x, y) => x + y, 0) / xs.length;
  })]);
  return { columns: [...by, ...aggs.map((a) => (a.fn === "count" ? "count" : `${a.fn}(${a.field})`))], rows: rows.slice(0, q.limit ?? 100) };
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
        if (req.url.startsWith("/v1/app/checks")) {
          let raw = "";
          req.on("data", (c: Buffer) => (raw += c));
          req.on("end", () => {
            const [, , , , id, sub] = req.url!.split("?")[0].split("/");   // /v1/app/checks[/id[/run]]
            const body = raw ? JSON.parse(raw) : {};
            const steps = (body.steps ?? CHECKS.find((x) => x.id === id)?.steps ?? []) as { name: string; url: string; extract?: { name: string }[] }[];
            const result = { ok: true, failure: null, failed_step: null, total_ms: 150.3 * steps.length, tls_days: 61.4,
              steps: steps.map((st, i) => ({ name: st.name, ok: true, failure: null, status: 200, url: st.url, extracted: (st.extract ?? []).map((e) => e.name),
                timings: { dns_ms: i ? 0.4 : 12.1, connect_ms: 31.6, tls_ms: 58.2, ttfb_ms: 141.7, total_ms: 150.3 }, body_sample: null })) };
            let out: unknown = { error: "no such check" }, status = 200;
            const c = CHECKS.find((x) => x.id === id);
            if (!id && req.method === "GET") out = { checks: CHECKS, limit: 20 };
            else if (!id && req.method === "POST") { const n = { ...body, id: hex(Math.random, 12), created_at: new Date().toISOString() }; CHECKS.push(n); out = n; status = 201; }
            else if (id === "test") out = { result };
            else if (!c) status = 404;
            else if (sub === "run") out = { result };
            else if (req.method === "PUT") { Object.assign(c, body); out = c; }
            else if (req.method === "DELETE") { CHECKS.splice(CHECKS.indexOf(c), 1); out = { deleted: id }; }
            else out = c;
            res.statusCode = status;
            setTimeout(() => res.end(JSON.stringify(out)), 200);
          });
          return;
        }
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
