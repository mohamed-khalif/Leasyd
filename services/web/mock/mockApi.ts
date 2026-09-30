// A fake /v1/app backend for `npm run mock`: realistic-looking answers to the
// same queries the UI sends, so the UI can be built and screenshotted
// without AWS. Deterministic (seeded) so screenshots are stable.
import { readFileSync } from "node:fs";
import type { Plugin } from "vite";

const SHOT = readFileSync(new URL("./shot.jpg", import.meta.url)).toString("base64");   // a browser check's screenshot

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
  match?: Record<string, string>; limit?: number; collapse?: number };

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
  ["e5f6a1b2c3d4", "Checkout journey", [
    { name: "Open the shop", action: "navigate", url: "https://shop.example.com/" },
    { name: "Add shoes to cart", action: "click", selector: "text=Add to cart" },
    { name: "Log in", action: "click", selector: "#login" },
    { name: "Email", action: "type", selector: "#email", text: "monitor@shop.example.com" },
    { name: "Password", action: "type", selector: "#password", text: "{password}" },
    { name: "Submit", action: "press", selector: "#password", key: "Enter" },
    { name: "Place order", action: "click", selector: "button:has-text('Place order')" },
    { name: "Order confirmed", action: "assert_text", text: "Thank you for your order" },
  ], 5, 0.972, 4200],
].map(([id, name, steps, frequency, up, ms]) => ({ id: id as string, name: name as string, steps, frequency: frequency as number,
  ...(id === "e5f6a1b2c3d4" ? { type: "browser", device: "desktop", screenshots: "failure", verify_tls: true } : { type: "http" }),
  timeout_ms: id === "e5f6a1b2c3d4" ? 45000 : 20000, variables: id === "b2c3d4e5f6a1" ? { base: "https://api.shop.example.com" } : {}, enabled: id !== "d4e5f6a1b2c3",
  secret_names: id === "b2c3d4e5f6a1" || id === "e5f6a1b2c3d4" ? ["password"] : [], created_at: "2026-09-20T10:00:00Z", updated_at: "2026-09-20T10:00:00Z", up, ms }));

/** A browser check's result: every step passes but the last, which fails with a screenshot. */
function browserResult(steps: { name: string; action: string; url?: string }[]) {
  const n = steps.length;
  return { ok: false, failure: `${steps[n - 1].name}: text 'Thank you for your order' not found`, failed_step: n - 1, tls_days: null, run_id: "f".repeat(32),
    total_ms: 4180.4, steps: steps.map((st, i) => {
      const last = i === n - 1, nav = st.action === "navigate";
      return { name: st.name, action: st.action, ok: !last, failure: last ? "text 'Thank you for your order' not found" : null, status: nav ? 200 : null,
        url: "https://shop.example.com/checkout", extracted: [], timings: { total_ms: nav ? 1320.5 : last ? 1500.2 : 180.4 },
        vitals: nav ? { ttfb_ms: 212.4, fcp_ms: 640.1, lcp_ms: 1184.0, cls: 0.021, dom_ms: 890.3, load_ms: 1290.8, transfer_bytes: 48213 } : null,
        console_errors: last ? ["POST https://pay.shop.example.com/v2/charge 503 (Service Unavailable)"] : [],
        http_errors: last ? ["503 https://pay.shop.example.com/v2/charge"] : [], failed_requests: [], blocked: [], screenshot: last ? SHOT : null };
    }) };
}

// Excluded runs, maintenance windows and SLOs, kept in memory.
const EXCLUSIONS: { check: string; run_id: string; reason: string; by: string; at: string }[] = [];
const SETTINGS: Record<string, Record<string, unknown>[]> = {
  windows: [{ id: "w1a2b3c4d5e6", name: "Weekly deployment", checks: ["*"],
              schedule: { type: "weekly", days: ["tue", "thu"], start: "22:00", duration_minutes: 60, timezone: "Europe/London" } }],
  slos: [
    { id: "s1a2b3c4d5e6", name: "Checkout available", description: "Customers can complete a purchase.", type: "availability",
      checks: ["e5f6a1b2c3d4", "b2c3d4e5f6a1"], target: 99.5, window_days: 30 },
    { id: "s2b3c4d5e6f1", name: "Homepage fast", description: "", type: "performance", checks: ["a1b2c3d4e5f6"], target: 99, window_days: 7, threshold_ms: 300 },
  ],
};

const CHANNELS: Record<string, unknown>[] = [
  { id: "c1a2b3c4d5e6", name: "On-call", type: "email", email: "oncall@acme.io", status: "confirmed" },
  { id: "c2b3c4d5e6f1", name: "#ops-alerts", type: "slack", url_hint: "https://hooks.slack.com/…x9Qz" },
];
const RULES: Record<string, unknown>[] = [
  { id: "r1a2b3c4d5e6", name: "Checkout is down", type: "check_failing", checks: ["e5f6a1b2c3d4", "b2c3d4e5f6a1"], failures: 2,
    channels: ["c1a2b3c4d5e6", "c2b3c4d5e6f1"], enabled: true, firing: ["e5f6a1b2c3d4"] },
  { id: "r2b3c4d5e6f1", name: "Checkout budget", type: "slo_burn", slo: "s1a2b3c4d5e6", burn_rate: 10, budget_below: 25,
    channels: ["c2b3c4d5e6f1"], enabled: true, firing: [] },
];

function alertsRoute(path: string, method: string, body: Record<string, unknown>): [number, unknown] {
  const [kind, id, sub] = path.split("/");
  const list = kind === "channels" ? CHANNELS : RULES;
  if (!id && method === "GET") return [200, { items: list, limit: 20 }];
  if (!id && method === "POST") {
    const n: Record<string, unknown> = { ...body, id: hex(Math.random, 12) };
    if (kind === "channels" && body.type === "email") n.status = "waiting for confirmation";
    if (kind === "channels" && body.type !== "email") { n.url_hint = `https://${String(body.url).split("/")[2]}/…${String(body.url).slice(-4)}`; delete n.url; }
    if (kind === "channels" && body.type === "webhook") n.signing_secret = hex(Math.random, 48);
    list.push(n); return [201, n];
  }
  const one = list.find((x) => x.id === id);
  if (!one) return [404, { error: "not found" }];
  if (sub === "test") return [200, { sent: true, error: null }];
  if (method === "PUT") { Object.assign(one, body); return [200, one]; }
  if (method === "DELETE") { list.splice(list.indexOf(one), 1); return [200, { deleted: id }]; }
  return [200, one];
}

function alertHistory(q: Q) {
  const t1 = Date.parse(q.end), cols = ["ts", "service", "severity_text", "body", "attributes"];
  const ev = [[95, "firing", "Checkout is down", "Checkout journey", "Failed 2 runs in a row. Last failure: Order confirmed: text 'Thank you for your order' not found"],
              [80, "resolved", "Checkout is down", "Checkout journey", "The check passed again."],
              [30, "firing", "Checkout is down", "Checkout journey", "Failed 2 runs in a row. Last failure: Place order: timed out waiting for locator(\"button:has-text('Place order')\")"]];
  return { columns: cols, rows: ev.filter(([m]) => t1 - Number(m) * 60000 > Date.parse(q.start)).map(([m, st, rule, subj, detail]) => [
    new Date(t1 - Number(m) * 60000).toISOString(), "alerts", st === "firing" ? "WARN" : "INFO", `${String(st).toUpperCase()}: ${rule} — ${subj}. ${detail}`,
    { "alert.state": st, "alert.rule": rule, "alert.subject": subj, "alert.url": "https://app.leasyd.com/#/synthetics/e5f6a1b2c3d4" }]) };
}

function settingsRoute(kind: string, id: string | undefined, method: string, body: Record<string, unknown>): [number, unknown] {
  const list = SETTINGS[kind], one = list.find((x) => x.id === id);
  if (!id && method === "GET") return [200, { items: list, limit: 20 }];
  if (!id && method === "POST") { const n = { ...body, id: hex(Math.random, 12) }; list.push(n); return [201, n]; }
  if (!one) return [404, { error: "not found" }];
  if (method === "PUT") { Object.assign(one, body); return [200, one]; }
  if (method === "DELETE") { list.splice(list.indexOf(one), 1); return [200, { deleted: id }]; }
  return [200, one];
}

function synthetics(q: Q) {
  const t0 = Date.parse(q.start), t1 = Date.parse(q.end);
  const metric = String(q.where?.find((w) => w.field === "metric_name")?.value ?? "");
  const only = q.where?.find((w) => w.field === "attributes.check.id")?.value;
  const cs = CHECKS.filter((c) => c.enabled && (!only || (Array.isArray(only) ? only.includes(c.id) : c.id === only)));
  const within = Number(q.where?.find((w) => w.field === "value" && w.op === "<=")?.value ?? NaN);   // an SLO's "fast enough" runs
  const r = rng(Math.floor(t1 / 60000));
  const fail = (c: MockCheck, t: number) => Number(c.up) < 1 && Math.sin(t / 7e5 + c.id.charCodeAt(0)) > 0.93;
  if (q.search && q.match?.trace_id) return runSpans(q.match.trace_id);
  if (q.search) {
    const rows = [];
    for (const c of cs) for (let t = t1 - 30000; t > t0 && rows.length < q.search.limit; t -= c.frequency * 60000) {
      const bad = fail(c, t), ms = Number(c.ms) * (0.8 + r() * 0.5) * (bad ? 6 : 1);
      rows.push({ ts: new Date(t).toISOString(), service: "synthetics", name: c.name, trace_id: `${c.id}${String(t).padStart(20, "0")}`, span_id: hex(r, 16),
        duration_ns: Math.round(ms * 1e6), status_code: bad ? 2 : 0,
        attributes: { "check.id": c.id, "check.name": c.name, "check.result": bad ? "fail" : "pass", "check.total_ms": Math.round(ms),
          "check.run_id": `${c.id}${String(t).padStart(20, "0")}`, "http.response.status_code": bad && c.type !== "browser" ? 503 : c.type === "browser" ? undefined : 200,
          ...(rows.length === 3 ? { "check.excluded": "maintenance window: Weekly deployment" } : {}),
          ...(bad && c.type === "browser" ? { "check.failed_step": 8, "check.failure": "Order confirmed: text 'Thank you for your order' not found", "check.screenshots": "8" }
            : bad ? { "check.failed_step": 2, "check.failure": "Create cart: status 503, expected 201" } : {}) } });
    }
    const cols = rows.length ? Object.keys(rows[0]) : ["ts"];
    return { columns: cols, rows: rows.map((x) => cols.map((k) => (x as Record<string, unknown>)[k])) };
  }
  const by = q.group_by ?? [], aggs = q.aggs ?? [{ fn: "count" }];
  const tsg = by.find((g) => g.startsWith("ts:")), b = tsg ? Number(tsg.slice(3)) * 1000 : t1 - t0;
  const rows: unknown[][] = [];
  if (by.includes("value") && tsg) {          // runs per bucket that passed (1) / failed (0)
    for (let t = Math.floor(t0 / b) * b; t <= t1; t += b) {
      const runs = cs.reduce((a, c) => a + (b / 60000) / c.frequency, 0), bad = cs.filter((c) => fail(c, t)).length * (b / 60000);
      rows.push([new Date(t).toISOString().replace("Z", "000Z"), 1, Math.round(runs - bad)]);
      if (bad) rows.push([new Date(t).toISOString().replace("Z", "000Z"), 0, Math.round(bad)]);
    }
    return { columns: [...by, "count"], rows };
  }
  if (q.where?.some((w) => w.field === "value" && w.op === "=") && by[0] === "attributes.check.id") {
    return { columns: ["attributes.check.id", "count"], rows: [["b2c3d4e5f6a1", 9], ["e5f6a1b2c3d4", 2]] };
  }
  for (const c of cs) for (let t = Math.floor(t0 / b) * b; t <= t1; t += b) {
    const up = Number(c.up) + (tsg && fail(c, t) ? -0.3 : 0), ms = Number(c.ms) * (0.9 + 0.2 * Math.sin(t / 3e6 + c.frequency));
    const runs = Math.round((b / 60000) / c.frequency);
    const v = (a: { fn: string }) => a.fn === "count" ? (Number.isNaN(within) ? runs : Math.round(runs * (within >= ms * 1.3 ? 0.998 : within >= ms ? 0.9 : 0.3)))
      : a.fn === "sum" && metric.endsWith("success") ? runs * Math.min(1, up)
      : metric.endsWith("success") ? up : metric.endsWith("tls_days_remaining") ? 58 + c.frequency
      : metric.endsWith("browser.lcp") ? (a.fn === "p95" ? 1.5 : 1) * 1180 : a.fn === "p95" ? ms * 1.6 : ms;
    if (by.includes("attributes.step.index") && metric.endsWith("browser.lcp")) {   // only pages opened have a largest paint
      rows.push([1, ...aggs.map(() => 1180)]);
      break;
    }
    if (by.includes("attributes.step.index")) {
      (c.steps as unknown[]).forEach((_, i) => rows.push([i + 1, ...aggs.map((a) => (a.fn === "p95" ? 1.6 : 1) * Number(c.ms) / (c.steps as unknown[]).length * (1 + i * 0.3))]));
      break;
    }
    rows.push([...by.map((g) => g.startsWith("ts:") ? new Date(t).toISOString().replace("Z", "000Z") : c.id), ...aggs.map(v)]);
    if (!tsg) break;
  }
  if (!by.includes("attributes.check.id") && !by.includes("attributes.step.index")) {   // several checks: one row per group, as the engine answers
    const merged = new Map<string, unknown[]>();
    for (const row of rows) {
      const k = JSON.stringify(row.slice(0, by.length)), m = merged.get(k);
      if (!m) { merged.set(k, [...row]); continue; }
      aggs.forEach((a, i) => { const j = by.length + i; m[j] = ["count", "sum"].includes(a.fn) ? Number(m[j]) + Number(row[j]) : (Number(m[j]) + Number(row[j])) / 2; });
    }
    rows.splice(0, rows.length, ...merged.values());
  }
  return { columns: [...by, ...aggs.map((a) => (a.fn === "count" ? "count" : `${a.fn}(${a.field})`))], rows };
}

/** One run's spans: the root and a span per step, with each step's request and response. */
function runSpans(trace: string) {
  const c = CHECKS.find((x) => trace.startsWith(x.id)) ?? CHECKS[0];
  const t = Number(trace.slice(12)) || Date.now(), bad = Number(c.up) < 1 && Math.sin(t / 7e5 + c.id.charCodeAt(0)) > 0.93;
  const steps = c.steps as { name: string; method?: string; url?: string; action?: string; body?: string }[];
  const failAt = bad ? (c.type === "browser" ? steps.length - 1 : 1) : -1;
  const rows: Record<string, unknown>[] = [];
  let at = t;
  steps.slice(0, failAt >= 0 ? failAt + 1 : steps.length).forEach((st, i) => {
    const ms = Number(c.ms) / steps.length * (0.8 + (i % 3) * 0.2) * (i === failAt ? 3 : 1), fail = i === failAt;
    const url = String(st.url ?? "").replace("{base}", "https://api.shop.example.com").replace("{cart_id}", "c_81f2");
    const status = fail ? 503 : st.method === "POST" && i === 1 ? 201 : 200;
    const body = st.name === "Log in" ? '{"access_token": "••••", "expires_in": 3600, "user": {"id": 42, "name": "Monitor"}}'
      : st.name === "Create cart" ? (fail ? '{"error": "service unavailable", "retry_after": 5}' : '{"cart": {"id": "c_81f2", "items": [{"sku": "OLJCESPC7Z", "qty": 1}]}}')
      : st.name === "Get cart" ? '{"cart": {"id": "c_81f2", "items": [{"sku": "OLJCESPC7Z", "qty": 1, "price": 101.96}]}}'
      : "<!doctype html><html><head><title>Shop</title></head><body><h1>Welcome</h1><button>Add to cart</button>…";
    rows.push({ ts: new Date(at).toISOString(), ts_unix_nano: at * 1e6, service: "synthetics", name: st.name, trace_id: trace, span_id: hex(rng(i + 7), 16),
      parent_span_id: "root", duration_ns: Math.round(ms * 1e6), status_code: fail ? 2 : 0,
      attributes: { "check.id": c.id, "step.index": i + 1, "step.name": st.name, "url.full": url || "https://shop.example.com/",
        "step.result": fail ? "fail" : "pass", ...(fail ? { "step.failure": c.type === "browser" ? "text 'Thank you for your order' not found" : "status 503, expected 201" } : {}),
        "step.dns_ms": 3.1, "step.connect_ms": 11.4, "step.tls_ms": 32.8, "step.ttfb_ms": ms * 0.8, "step.total_ms": ms,
        ...(c.type === "browser" ? { "step.action": st.action, ...(fail ? { "step.screenshot": true } : {}) } : {
          "http.response.status_code": status, "http.request.method": st.method ?? "GET",
          "http.request.headers": JSON.stringify({ "User-Agent": "Leasyd-Synthetics/1.0", Accept: "*/*", ...(i ? { Authorization: "••••" } : { Authorization: "••••", "Content-Type": "application/json" }) }),
          ...(st.body ? { "http.request.body": st.body } : {}),
          "http.response.headers": JSON.stringify({ "content-type": body.startsWith("{") ? "application/json" : "text/html; charset=utf-8", "content-length": String(body.length),
            date: new Date(at).toUTCString(), server: "envoy", "x-request-id": hex(rng(i), 12), ...(i === 0 && st.name === "Log in" ? { "set-cookie": "••••" } : {}) }),
          "http.response.body": body }) } });
    at += ms;
  });
  const cols = Object.keys(rows[0]);
  return { columns: cols, rows: rows.map((x) => cols.map((k) => x[k])) };
}

function answer(q: Q) {
  if (q.services?.includes("alerts")) return alertHistory(q);
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
    * (text ? 0.04 : 1) * (q.where?.some((w) => w.field === "status_code" && Number(w.value) === 2) ? 0.012 : 1);

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
    const base = 18e6 * (1 + (Math.max(0, SERVICES.indexOf(String(c.key[0]))) % 5)) * (0.7 + r() * 0.6);
    return base * (({ p50: 1, p90: 2.6, p95: 3.4, p99: 6.8 } as Record<string, number>)[a.fn] ?? 1);
  })]).filter((row) => Number(row[by.length]) > 0);
  const lb = by.indexOf("log:duration_ns"), sc = by.indexOf("status_code");
  if (lb >= 0 && sc >= 0) for (const row of rows) if (row[sc] === 2) row[by.length] = Math.round(Number(row[by.length]) * (Number(row[lb]) >= 56 ? 6 : 0));
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
  if (g === "severity_number") return [[9, .72], [5, .14], [13, .09], [17, .045], [21, .005]].filter(([n]) => sevOk(({ 5: "DEBUG", 9: "INFO", 13: "WARN", 17: "ERROR", 21: "ERROR" } as Record<number, string>)[n]))
    .map(([v, s]) => ({ value: v, share: s }));
  if (g === "log:duration_ns") return Array.from({ length: 40 }, (_, i) => 22 + i).map((b) => ({ value: b, share: Math.exp(-((b - 42) ** 2) / 14) / 6.6 + (b > 54 ? 0.003 : 0) }));
  if (g === "status_code") return [[0, .96], [2, .04]].map(([v, s]) => ({ value: v, share: s }));
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
  if (q.collapse === 1 && by[0] === "metric_name") {   // series per metric
    return { columns: ["metric_name", "groups", "count"], rows: defs.map((d) => [d.name, d.svcs.filter((s) => svcs.includes(s)).length * (1 + d.name.length % 7) * 3,
                                                                                   Math.round(d.svcs.length * (t1 - t0) / 10000)]) };
  }
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
      ...span(r, t1 - i * 7000, SERVICES[Math.floor(r() ** 2 * 7)], hex(r, 32), hex(r, 16), i % 3 ? hex(r, 16) : null, 2e6 + r() ** 3 * 2.4e9),
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
    attributes: svc === "cart" ? { "db.system": "redis", "db.operation.name": "HGET" }
      : svc.startsWith("frontend") ? { "http.request.method": "GET", "http.route": "/api/products" }
      : { "rpc.system": "grpc", "rpc.service": "oteldemo" },
    resource_attributes: { "service.name": svc },
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
        const al = req.url.split("?")[0].match(/^\/v1\/app\/alerts\/(.+)$/);
        if (al) {
          let raw = "";
          req.on("data", (c: Buffer) => (raw += c));
          req.on("end", () => {
            const [status, out] = alertsRoute(al[1], req.method ?? "GET", raw ? JSON.parse(raw) : {});
            res.statusCode = status;
            setTimeout(() => res.end(JSON.stringify(out)), 150);
          });
          return;
        }
        const settings = req.url.split("?")[0].match(/^\/v1\/app\/(windows|slos)(?:\/([a-z0-9]+))?$/);
        if (settings) {
          let raw = "";
          req.on("data", (c: Buffer) => (raw += c));
          req.on("end", () => {
            const [status, out] = settingsRoute(settings[1], settings[2], req.method ?? "GET", raw ? JSON.parse(raw) : {});
            res.statusCode = status;
            setTimeout(() => res.end(JSON.stringify(out)), 150);
          });
          return;
        }
        if (req.url.startsWith("/v1/app/checks")) {
          let raw = "";
          req.on("data", (c: Buffer) => (raw += c));
          req.on("end", () => {
            const [, , , , id, sub, run] = req.url!.split("?")[0].split("/");   // /v1/app/checks[/id[/run | /exclusions[/run]]]
            const body = raw ? JSON.parse(raw) : {};
            const steps = (body.steps ?? CHECKS.find((x) => x.id === id)?.steps ?? []) as { name: string; url: string; extract?: { name: string }[] }[];
            const isBrowser = (body.type ?? CHECKS.find((x) => x.id === id)?.type) === "browser";
            const result = isBrowser ? browserResult(steps as unknown as { name: string; action: string; url?: string }[])
              : { ok: true, failure: null, failed_step: null, total_ms: 150.3 * steps.length, tls_days: 61.4,
              steps: steps.map((st, i) => ({ name: st.name, ok: true, failure: null, status: 200, url: st.url, extracted: (st.extract ?? []).map((e) => e.name),
                timings: { dns_ms: i ? 0.4 : 12.1, connect_ms: 31.6, tls_ms: 58.2, ttfb_ms: 141.7, total_ms: 150.3 }, body_sample: null })) };
            let out: unknown = { error: "no such check" }, status = 200;
            const c = CHECKS.find((x) => x.id === id);
            if (!id && req.method === "GET") out = { checks: CHECKS.map((x) => ({ ...x, excluded_runs: EXCLUSIONS.filter((e) => e.check === x.id).map((e) => e.run_id) })), limit: 20 };
            else if (!id && req.method === "POST") { const n = { ...body, id: hex(Math.random, 12), created_at: new Date().toISOString() }; CHECKS.push(n); out = n; status = 201; }
            else if (id === "test") out = { result };
            else if (!c) status = 404;
            else if (sub === "run") out = { result };
            else if (sub === "screenshot") out = { image: SHOT, content_type: "image/jpeg" };
            else if (sub === "exclusions" && req.method === "POST") { const e = { check: id, ...body, by: "ana@acme.io", at: new Date().toISOString() }; EXCLUSIONS.push(e); out = e; status = 201; }
            else if (sub === "exclusions" && req.method === "DELETE") { EXCLUSIONS.splice(EXCLUSIONS.findIndex((e) => e.run_id === run), 1); out = { included: run }; }
            else if (req.method === "PUT") { Object.assign(c, body); out = c; }
            else if (req.method === "DELETE") { CHECKS.splice(CHECKS.indexOf(c), 1); out = { deleted: id }; }
            else out = { ...c, exclusions: EXCLUSIONS.filter((e) => e.check === id) };
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
