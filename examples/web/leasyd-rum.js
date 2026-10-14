/*! Leasyd web monitoring (real users). https://www.leasyd.com/docs#web
 *
 * <script src="https://app.leasyd.com/install/leasyd-rum.js" defer
 *         data-key="pub_..." data-app="shop-web" data-env="prod"
 *         data-trace-origins="https://api.example.com"></script>
 *
 * Collects, per page: page views and load timing, Core Web Vitals (LCP, CLS, INP, FCP, TTFB), JavaScript
 * errors, and API calls (fetch / XHR) that fail or are slow. Calls to data-trace-origins (and to the page's
 * own origin) carry a W3C traceparent header, so a slow page links to the backend trace it caused.
 * No cookies, no personal data: URLs are sent without their query string or fragment; a session is a random
 * id kept in this browser's storage until 30 minutes without activity.
 * The key is a public browser key (Settings > Browser keys): it can only send web events, from the
 * origins you allow, and nothing else.
 */
(function () {
  "use strict";
  var me = document.currentScript;
  if (!me || window.__leasydRum) return;
  window.__leasydRum = true;
  var KEY = me.getAttribute("data-key") || "";
  var APP = me.getAttribute("data-app") || location.hostname;
  var ENV = me.getAttribute("data-env") || "";
  var ENDPOINT = me.getAttribute("data-endpoint") || "https://ingest.leasyd.com/v1/rum";
  var SAMPLE = parseFloat(me.getAttribute("data-sample") || "1");
  var SLOW_MS = parseInt(me.getAttribute("data-slow-call-ms") || "1000", 10);
  var TRACE = (me.getAttribute("data-trace-origins") || "").split(/[\s,]+/).filter(Boolean).concat([location.origin]);
  if (!KEY || !window.JSON || Math.random() >= SAMPLE) return;

  // ---- session: random id, renewed after 30 minutes without activity
  var session = (function () {
    var id, now = Date.now();
    try {
      var s = JSON.parse(localStorage.getItem("leasyd.rum") || "null");
      id = s && now - s.last < 30 * 60000 ? s.id : null;
    } catch (e) { /* storage blocked */ }
    id = id || hex(16);
    function touch() { try { localStorage.setItem("leasyd.rum", JSON.stringify({ id: id, last: Date.now() })); } catch (e) { /* ignore */ } }
    touch();
    return { id: id, touch: touch };
  })();

  function hex(bytes) {
    var a = new Uint8Array(bytes), s = "";
    (window.crypto || window.msCrypto).getRandomValues(a);
    for (var i = 0; i < a.length; i++) s += (a[i] < 16 ? "0" : "") + a[i].toString(16);
    return s;
  }
  function path(u) {
    try { var x = new URL(u, location.href); return x.origin === location.origin ? x.pathname : x.origin + x.pathname; } catch (e) { return ""; }
  }
  function round(n) { return Math.round(n * 1000) / 1000; }

  // ---- sending: batched; flushed every 5 s and when the page is hidden (sendBeacon survives unload)
  var queue = [], page = { id: hex(8), path: path(location.href) };
  function add(e) {
    if (queue.length >= 200) return;
    e.t = Date.now(); e.page = page.path; e.pv = page.id;
    queue.push(e); session.touch();
  }
  function flush() {
    if (!queue.length) return;
    var body = JSON.stringify({ k: KEY, app: APP, env: ENV, sid: session.id, events: queue.splice(0, 200) });
    var url = ENDPOINT + "?k=" + encodeURIComponent(KEY);
    // text/plain: a "simple" request, so no CORS preflight
    if (!(navigator.sendBeacon && navigator.sendBeacon(url, new Blob([body], { type: "text/plain" })))) {
      try { origFetch(url, { method: "POST", body: body, keepalive: true, headers: { "Content-Type": "text/plain" } }); } catch (e) { /* ignore */ }
    }
  }
  setInterval(flush, 5000);
  addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") { reportVitals(); flush(); } });
  addEventListener("pagehide", function () { reportVitals(); flush(); });

  // ---- page views (the first load, then single-page-app route changes)
  function pageView(spa) {
    var e = { type: "page_view", title: (document.title || "").slice(0, 200), spa: !!spa };
    var ref = document.referrer && path(document.referrer);
    if (ref && ref.indexOf("http") === 0) e.referrer = ref.split("/").slice(0, 3).join("/");
    if (!spa) {
      var n = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
      if (n) {
        e.ttfb_ms = round(n.responseStart); e.dom_ms = round(n.domContentLoadedEventEnd);
        e.load_ms = round(n.loadEventEnd || 0); e.transfer_bytes = n.transferSize || 0; e.nav = n.type;
      }
    }
    add(e);
  }
  function routeChanged() {
    var p = path(location.href);
    if (p === page.path) return;
    reportVitals();
    page = { id: hex(8), path: p };
    resetVitals();
    pageView(true);
  }
  ["pushState", "replaceState"].forEach(function (m) {
    var orig = history[m];
    history[m] = function () { var r = orig.apply(this, arguments); setTimeout(routeChanged, 0); return r; };
  });
  addEventListener("popstate", routeChanged);
  if (document.readyState === "complete") setTimeout(function () { pageView(false); }, 0);
  else addEventListener("load", function () { setTimeout(function () { pageView(false); }, 0); });

  // ---- Core Web Vitals (thresholds from web.dev: good / needs improvement / poor)
  var LIMITS = { LCP: [2500, 4000], CLS: [0.1, 0.25], INP: [200, 500], FCP: [1800, 3000], TTFB: [800, 1800] };
  var vitals, sent;
  function resetVitals() { vitals = {}; sent = {}; }
  resetVitals();
  function vital(name, value, final) {
    vitals[name] = value;
    if (final) send(name);
  }
  function send(name) {
    if (sent[name] || vitals[name] == null) return;
    sent[name] = true;
    var v = vitals[name], l = LIMITS[name];
    add({ type: "web_vital", name: name, value: round(v), rating: v <= l[0] ? "good" : v <= l[1] ? "needs-improvement" : "poor" });
  }
  function reportVitals() { for (var n in vitals) send(n); }
  function observe(type, cb, opts) {
    try {
      var o = new PerformanceObserver(function (l) { l.getEntries().forEach(cb); });
      o.observe(Object.assign({ type: type, buffered: true }, opts || {}));
    } catch (e) { /* not supported in this browser */ }
  }
  var nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
  if (nav && nav.responseStart > 0) vital("TTFB", nav.responseStart, true);
  observe("paint", function (e) { if (e.name === "first-contentful-paint") vital("FCP", e.startTime, true); });
  observe("largest-contentful-paint", function (e) { vital("LCP", e.startTime); });
  var cls = 0, win = 0, winStart = 0, winLast = 0;
  observe("layout-shift", function (e) {
    if (e.hadRecentInput) return;
    if (win && e.startTime - winLast < 1000 && e.startTime - winStart < 5000) win += e.value;
    else { win = e.value; winStart = e.startTime; }
    winLast = e.startTime;
    if (win > cls) { cls = win; vital("CLS", cls); }
  });
  observe("event", function (e) {
    if (e.interactionId && (vitals.INP == null || e.duration > vitals.INP)) vital("INP", e.duration);
  }, { durationThreshold: 40 });

  // ---- JavaScript errors
  var errors = 0;
  function error(kind, msg, src, line, col, stack) {
    if (++errors > 25) return;
    add({ type: "js_error", kind: kind, message: String(msg || "").slice(0, 500), source: src ? path(src) : "",
          line: line || 0, column: col || 0, stack: String(stack || "").slice(0, 2000) });
  }
  addEventListener("error", function (e) {
    if (e.error || e.message) error("error", e.message, e.filename, e.lineno, e.colno, e.error && e.error.stack);
  });
  addEventListener("unhandledrejection", function (e) {
    var r = e.reason || {};
    error("unhandledrejection", r.message || r, "", 0, 0, r.stack);
  });

  // ---- API calls: failures and slow ones; traced ones carry traceparent
  function traced(u) {
    try { var o = new URL(u, location.href).origin; return TRACE.indexOf(o) >= 0; } catch (e) { return false; }
  }
  function call(method, u, status, started, traceId) {
    var ms = performance.now() - started;
    if (status >= 400 || status === 0 || ms >= SLOW_MS) {     // only these are recorded (each event is a billed record)
      add({ type: "api_call", method: method, url: path(u), status: status, duration_ms: round(ms), trace_id: traceId || "" });
    }
  }
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var u = typeof input === "string" ? input : (input && input.url) || "";
      if (u.indexOf(ENDPOINT) === 0) return origFetch.apply(this, arguments);
      var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
      var started = performance.now(), traceId = "";
      if (traced(u)) {
        traceId = hex(16);
        init = Object.assign({}, init || {});
        var h = new Headers(init.headers || (input && input.headers) || {});
        h.set("traceparent", "00-" + traceId + "-" + hex(8) + "-01");
        init.headers = h;
      }
      return origFetch.call(this, input, init).then(function (r) {
        call(method, u, r.status, started, traceId); return r;
      }, function (err) { call(method, u, 0, started, traceId); throw err; });
    };
  }
  var X = window.XMLHttpRequest && XMLHttpRequest.prototype;
  if (X) {
    var open = X.open, sendX = X.send;
    X.open = function (m, u) { this.__l = { m: String(m).toUpperCase(), u: String(u) }; return open.apply(this, arguments); };
    X.send = function () {
      var l = this.__l, xhr = this;
      if (l && l.u.indexOf(ENDPOINT) !== 0) {
        l.s = performance.now();
        if (traced(l.u)) { l.tid = hex(16); try { xhr.setRequestHeader("traceparent", "00-" + l.tid + "-" + hex(8) + "-01"); } catch (e) { /* ignore */ } }
        xhr.addEventListener("loadend", function () { call(l.m, l.u, xhr.status, l.s, l.tid); });
      }
      return sendX.apply(this, arguments);
    };
  }

  window.leasyd = { flush: flush };
})();
