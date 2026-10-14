"""Web monitoring (real users): POST /v1/rum from the browser script (examples/web/leasyd-rum.js).

Public route (no API key authorizer): the body or ?k= carries a public browser key (scope "browser",
made in Settings) that names the tenant and the origins allowed to use it. The request's Origin (or
Referer) must be one of them. Events become ordinary OTLP log records of the tenant (service.name =
the app), so drop rules, metering, daily caps, search, PromQL and the AI SRE all apply as for any log:

  resource: service.name (data-app), deployment.environment (data-env), telemetry.sdk.name=leasyd-rum,
            browser.name, browser.version, os.name, device.type (from the User-Agent)
  record:   body "page_view /checkout" | "LCP 2400 ms good /" | "js_error <message>" | "GET /api/cart 503 1200 ms"
            severity INFO; WARN for a poor vital or a 4xx call; ERROR for a JavaScript error or a 5xx/failed call
            attributes rum.type (page_view | web_vital | js_error | api_call), rum.session_id, rum.page_view_id,
            url.path, and the event's own fields (rum.vital.name/value/rating, rum.load_ms, error.message, ...)
            trace_id: an API call's, so its log links to the backend trace
"""

import json
import re
import time

MAX_BODY = 64 * 1024
MAX_EVENTS = 200
KEY_TTL_S = 60
_KEY = re.compile(r"^pub_[A-Za-z0-9]{20,64}$")
_APP = re.compile(r"[^A-Za-z0-9._ -]+")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
TYPES = {"page_view", "web_vital", "js_error", "api_call"}
VITALS = {"LCP", "CLS", "INP", "FCP", "TTFB"}
_keys = {}   # key -> (expires, item or None): browser keys are looked up on every beacon


def handle(event, ingest):
    """-> API Gateway response. `ingest` is the ingest module (meter, accept, reject_out_of_range...)."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    origin = _origin(headers)
    if event.get("httpMethod") == "OPTIONS":
        return _cors(204, origin, None, preflight=True)
    raw = ingest._body(event, headers)
    if len(raw) > MAX_BODY:
        return _cors(413, origin, f"at most {MAX_BODY // 1024} KB per request")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _cors(400, origin, "body: JSON")
    if not isinstance(body, dict):
        return _cors(400, origin, "body: a JSON object")
    key = str((event.get("queryStringParameters") or {}).get("k") or body.get("k") or "")
    item = _key(key, ingest)
    if not item:
        return _cors(401, origin, "unknown or revoked browser key")
    if not origin or origin not in (item.get("origins") or []):
        print(json.dumps({"rum_refused": "origin", "tenant": item["tenant"], "origin": (origin or "")[:100]}))
        return _cors(403, origin, "this page's origin isn't allowed for the browser key (Settings > Browser keys)")
    tenant = item["tenant"]
    if ingest.meter.closed(tenant) or ingest.meter.over_cap(tenant):
        ingest.meter.add(tenant, 0, 0, refused=len(raw))
        return _cors(429, origin, "not accepting data for this account now")
    doc = to_otlp(body, headers.get("user-agent", ""))
    if not doc["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
        return _cors(200, origin, None)
    rejected, _ = ingest.reject_out_of_range("logs", doc)
    out = ingest.accept(tenant, "logs", doc, drop=True, rejected=rejected)
    print(json.dumps({"tenant": tenant, "signal": "rum", **out}))
    return _cors(200, origin, None)


def _origin(headers):
    o = (headers.get("origin") or "").strip().lower().rstrip("/")
    if o and o != "null":
        return o
    m = re.match(r"^(https?://[^/]+)", (headers.get("referer") or "").strip().lower())
    return m.group(1) if m else ""


def _key(key, ingest):
    if not _KEY.match(key):
        return None
    now = time.time()
    hit = _keys.get(key)
    if hit and hit[0] > now:
        return hit[1]
    raw = ingest.ddb.get_item(TableName=ingest.TENANTS_TABLE, Key={"pk": {"S": f"key#{_sha256(key)}"}}).get("Item") or {}
    item = {"tenant": raw.get("tenant", {}).get("S", ""), "scope": raw.get("scope", {}).get("S", ""),
            "status": raw.get("status", {}).get("S", ""),
            "origins": [o.get("S", "") for o in raw.get("origins", {}).get("L", [])]}
    ok = item["scope"] == "browser" and item["status"] == "active" and bool(ingest._TENANT.match(item["tenant"]))
    if len(_keys) > 1000:
        _keys.clear()
    _keys[key] = (now + KEY_TTL_S, item if ok else None)
    return item if ok else None


def _sha256(s):
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def _cors(status, origin, error, preflight=False):
    h = {"Content-Type": "application/json", "Vary": "Origin"}
    if origin:
        h["Access-Control-Allow-Origin"] = origin
    if preflight:
        h.update({"Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type",
                  "Access-Control-Max-Age": "86400"})
        return {"statusCode": status, "headers": h, "body": ""}
    return {"statusCode": status, "headers": h, "body": "{}" if error is None else json.dumps({"message": error})}


# ------------------------------------------------------------ events -> OTLP logs

def to_otlp(body, user_agent):
    app = _APP.sub("", str(body.get("app") or "web"))[:64].strip() or "web"
    res = {"service.name": app, "telemetry.sdk.name": "leasyd-rum", **browser(user_agent)}
    env = _APP.sub("", str(body.get("env") or ""))[:32]
    if env:
        res["deployment.environment"] = env
    session = _id(body.get("sid"))
    records = []
    for e in (body.get("events") or [])[:MAX_EVENTS]:
        if isinstance(e, dict) and e.get("type") in TYPES:
            r = record(e, session)
            if r:
                records.append(r)
    return {"resourceLogs": [{"resource": {"attributes": _attrs(res)},
                              "scopeLogs": [{"scope": {"name": "leasyd-rum"}, "logRecords": records}]}]}


def record(e, session):
    t = e.get("type")
    try:
        ts_ms = float(e.get("t") or time.time() * 1000)
    except (TypeError, ValueError):
        return None
    page = route(_text(e.get("page"), 300))
    a = {"rum.type": t, "rum.session_id": session, "rum.page_view_id": _id(e.get("pv")), "url.path": page}
    sev, trace = 9, ""
    if t == "page_view":
        a.update({"rum.title": _text(e.get("title"), 200), "rum.spa": bool(e.get("spa")), "rum.referrer": _text(e.get("referrer"), 200),
                  "rum.navigation": _text(e.get("nav"), 20)})
        for k in ("ttfb_ms", "dom_ms", "load_ms", "transfer_bytes"):
            if _num(e.get(k)) is not None:
                a[f"rum.{k}"] = _num(e.get(k))
        text = f"page_view {page}"
    elif t == "web_vital":
        name, value = str(e.get("name") or "").upper(), _num(e.get("value"))
        if name not in VITALS or value is None:
            return None
        rating = e.get("rating") if e.get("rating") in ("good", "needs-improvement", "poor") else ""
        a.update({"rum.vital.name": name, "rum.vital.value": value, "rum.vital.rating": rating})
        sev = 13 if rating == "poor" else 9
        text = f"{name} {value:g}{'' if name == 'CLS' else ' ms'} {rating} {page}"
    elif t == "js_error":
        msg = _text(e.get("message"), 500) or "Script error."
        a.update({"error.message": msg, "error.kind": _text(e.get("kind"), 40), "error.source": _text(e.get("source"), 300),
                  "error.line": int(_num(e.get("line")) or 0), "error.column": int(_num(e.get("column")) or 0),
                  "error.stack": _text(e.get("stack"), 2000)})
        sev, text = 17, f"js_error {msg}"
    else:   # api_call
        status, ms = int(_num(e.get("status")) or 0), _num(e.get("duration_ms")) or 0
        method, url = _text(e.get("method"), 10).upper() or "GET", route(_text(e.get("url"), 300))
        a.update({"http.request.method": method, "url.full": url, "http.response.status_code": status, "rum.duration_ms": ms})
        sev = 17 if status == 0 or status >= 500 else 13 if status >= 400 else 9
        trace = str(e.get("trace_id") or "").lower()
        text = f"{method} {url} {status or 'failed'} {ms:g} ms"
    rec = {"timeUnixNano": str(int(ts_ms * 1_000_000)), "observedTimeUnixNano": str(time.time_ns()),
           "severityNumber": sev, "severityText": {9: "INFO", 13: "WARN", 17: "ERROR"}[sev],
           "body": {"stringValue": text[:1000]}, "attributes": _attrs({k: v for k, v in a.items() if v not in ("", None)})}
    if _HEX32.match(trace):
        rec["traceId"] = trace
    return rec


_ID_SEG = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{6,})$", re.I)


def route(path):
    """/product/1YMWWN1N4O -> /product/{id}: pages group by route, not by each product, order or user."""
    head, sep, rest = path.partition("://")
    if sep:   # another origin: keep it, route its path
        host, _, p = rest.partition("/")
        return f"{head}://{host}" + route("/" + p)
    return "/".join("{id}" if _ID_SEG.match(seg) else seg for seg in path.split("/"))


def browser(ua):
    """A few coarse fields from the User-Agent (no fingerprinting): browser, version, OS, device type."""
    ua = ua or ""
    out = {}
    for name, pat in (("Edge", r"Edg/(\d+)"), ("Opera", r"OPR/(\d+)"), ("Samsung Internet", r"SamsungBrowser/(\d+)"),
                      ("Chrome", r"(?:Chrome|CriOS)/(\d+)"), ("Firefox", r"(?:Firefox|FxiOS)/(\d+)"),
                      ("Safari", r"Version/(\d+)[\d.]* (?:Mobile/\S+ )?Safari")):
        m = re.search(pat, ua)
        if m:
            out["browser.name"], out["browser.version"] = name, m.group(1)
            break
    for name, pat in (("iOS", r"iPhone|iPad|iPod"), ("Android", r"Android"), ("Windows", r"Windows"),
                      ("macOS", r"Mac OS X|Macintosh"), ("ChromeOS", r"CrOS"), ("Linux", r"Linux")):
        if re.search(pat, ua):
            out["os.name"] = name
            break
    out["device.type"] = ("tablet" if re.search(r"iPad|Tablet", ua) else "mobile" if re.search(r"Mobi|iPhone|Android", ua)
                          else "desktop" if ua else "unknown")
    return out


def _attrs(d):
    out = []
    for k, v in d.items():
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            out.append({"key": k, "value": {"doubleValue": v}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e12 else None


def _text(v, n):
    return str(v or "").replace("\x00", "")[:n]


def _id(v):
    s = str(v or "")
    return s if re.match(r"^[0-9a-f]{8,32}$", s) else ""
