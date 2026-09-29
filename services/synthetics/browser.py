"""Browser checks: a real headless Chromium walks through a customer's site, step by step.

Runs in its own Lambda (a container image with Chromium; see Dockerfile) whose role can do nothing
but write its own logs: it gets one check's settings and that check's decrypted secrets, and
returns the result (with screenshots). The runner (synthetics.run, or the API for "test" and
"run now") decrypts, invokes it and records the result as the tenant's telemetry, so a page that
took over the browser would find nothing worth taking. (Chromium can't use its sandbox in Lambda.)

Steps (validated by synthetics.validate_browser_step):
  navigate url            open a URL (and wait for the page's load event); fails on a 4xx/5xx page
  click / hover selector  a Playwright selector: CSS ("#login"), text="Sign in", role=button[name="Pay"]
  type selector text      fill a field; {name} is replaced by variables, secrets and extracted values
  select selector value   choose an option (by value or label)
  press key [selector]    a key such as Enter, Tab or Control+A
  wait_for selector       until the element is visible
  wait ms                 a fixed pause (at most 10 s)
  assert_text text [selector]   the text is visible (on the page, or inside the element)
  assert_no_text text           the text is not visible
  assert_element selector       the element is visible
  assert_url value              the current URL contains the value
  extract selector name [attribute]   the element's text (or attribute) into {name} for later steps
The first failing step ends the run.

Safety
- Every connection the browser makes (pages, images, scripts, XHR, WebSockets; HTTP and HTTPS)
  goes through a local proxy in this process that resolves the host itself and only connects to
  public addresses (safety.resolve), and to the address it checked. Chromium sends even loopback
  through it; QUIC and non-proxied WebRTC UDP are off, so nothing can go around it. Blocked
  requests are reported with the step.
- Secrets are typed into fields but masked in everything returned: URLs, messages, and screenshots
  (fields and text containing a secret are painted over).
- A fresh browser and profile per run; afterwards every process but this one is killed and /tmp
  is emptied, so nothing carries over to the next customer's run in the same Lambda environment.
"""

import base64
import os
import re
import select
import shutil
import signal
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit

import safety
from safety import MASK, Refused

# This function needs no AWS credentials: keep them out of Chromium's (and its driver's)
# environment. They stay readable in /proc, which is why the role is worth nothing.
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(_k, None)

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

ON_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
STEP_TIMEOUT_MS = 15_000
MAX_WAIT_MS = 10_000
VIEWPORTS = {"desktop": {"width": 1366, "height": 768}, "mobile": {"width": 390, "height": 844}}
USER_AGENT = {
    "desktop": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Leasyd-Synthetics/1.0",
    "mobile": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36 Leasyd-Synthetics/1.0",
}
JPEG_QUALITY = 55
ARGS = ["--disable-quic", "--disable-dev-shm-usage", "--disable-gpu", "--disable-background-networking",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--proxy-bypass-list=<-loopback>"]
if ON_LAMBDA:   # Lambda has no namespaces for Chromium's zygote (adjustable without a new image)
    ARGS += os.environ.get("CHROMIUM_LAMBDA_ARGS", "--no-zygote --single-process").split()

VITALS_JS = """() => new Promise(done => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  let lcp = null, cls = 0;
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) lcp = e.renderTime || e.loadTime || e.startTime; })
          .observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) cls += e.value; })
          .observe({type: 'layout-shift', buffered: true}); } catch (e) {}
  const paint = performance.getEntriesByName('first-contentful-paint')[0];
  setTimeout(() => done({ttfb_ms: nav.responseStart || null, fcp_ms: paint ? paint.startTime : null, lcp_ms: lcp, cls: cls,
                         dom_ms: nav.domContentLoadedEventEnd || null, load_ms: nav.loadEventEnd || null,
                         transfer_bytes: nav.transferSize || null}), 50);
})"""

# Marks the elements showing a secret (text or field value), so the screenshot paints over them.
MARK_JS = """(secrets) => {
  let n = 0;
  const has = s => s && secrets.some(v => s.includes(v));
  for (const el of document.querySelectorAll('input, textarea')) if (has(el.value)) { el.setAttribute('data-leasyd-mask', ''); n++; }
  const walk = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  while (walk.nextNode()) { const t = walk.currentNode; if (has(t.nodeValue) && t.parentElement) { t.parentElement.setAttribute('data-leasyd-mask', ''); n++; } }
  return n;
}"""
UNMARK_JS = "() => { for (const el of document.querySelectorAll('[data-leasyd-mask]')) el.removeAttribute('data-leasyd-mask'); }"


class StepFailed(Exception):
    pass


# ------------------------------------------------------------------ the proxy every connection goes through

class _Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self):
        proxy = self.server
        try:
            line = self.rfile.readline(8192).decode("latin-1").split()
            headers = []
            while True:
                h = self.rfile.readline(65536)
                if h in (b"\r\n", b"\n", b""):
                    break
                headers.append(h)
            if len(line) != 3:
                return
            method, where, version = line
            if method == "CONNECT":             # HTTPS and secure WebSockets: a tunnel to a checked address
                host, _, port = where.rpartition(":")
                upstream = proxy.connect(host, int(port))
                if upstream is None:
                    return self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                proxy.pipe(self.connection, upstream)
                return
            u = urlsplit(where)                  # plain HTTP: absolute URL in the request line
            if u.scheme != "http" or not u.hostname:
                return self.wfile.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            upstream = proxy.connect(u.hostname, u.port or 80)
            if upstream is None:
                return self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            out, length = [f"{method} {path} {version}\r\n".encode("latin-1")], 0
            for h in headers:
                name = h.split(b":", 1)[0].strip().lower()
                if name in (b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive"):
                    continue
                if name == b"transfer-encoding":   # streamed request bodies aren't supported
                    upstream.close()
                    return self.wfile.write(b"HTTP/1.1 501 Not Implemented\r\nContent-Length: 0\r\n\r\n")
                if name == b"content-length":
                    length = int(h.split(b":", 1)[1].strip() or 0)
                out.append(h)
            out.append(b"Connection: close\r\n\r\n")   # one request per connection: the next may be another host
            upstream.sendall(b"".join(out) + (self.rfile.read(length) if length else b""))
            proxy.pipe(self.connection, upstream)
        except (OSError, ValueError):
            pass


class VettingProxy(socketserver.ThreadingTCPServer):
    """An HTTP proxy on 127.0.0.1 that only connects to public addresses."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.port = self.server_address[1]
        self.blocked, self.lock, self.socks = [], threading.Lock(), set()
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def connect(self, host, port):
        try:
            host = safety.host_ok(host)
            ip = safety.resolve(host, port)[0]
            s = socket.create_connection((ip, port), timeout=15)
        except Refused as e:
            with self.lock:
                self.blocked.append({"host": host, "reason": str(e)})
            return None
        except OSError:
            return None
        with self.lock:
            self.socks.add(s)
        return s

    def pipe(self, a, b):
        try:
            while True:
                ready, _, _ = select.select([a, b], [], [], 30)
                if not ready:
                    return
                for s in ready:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            return
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass
            with self.lock:
                self.socks.discard(b)

    def stop(self):
        self.shutdown()
        self.server_close()
        with self.lock:
            for s in list(self.socks):
                try:
                    s.close()
                except OSError:
                    pass


# ------------------------------------------------------------------ running a check

def run_browser(check, secret_values, budget_s=None, screenshots=None):
    """Run a browser check's steps in order until one fails -> result dict (never raises).
    check: settings from synthetics.validate (type "browser"); secret_values: {name: plaintext}."""
    started = time.time()
    budget = min(check["timeout_ms"] / 1000, budget_s or 1e9)
    deadline = time.perf_counter() + budget
    shots = screenshots or check.get("screenshots", "failure")
    values = {**check.get("variables", {}), **secret_values}
    masked = [v for v in secret_values.values() if v]
    steps, failure, proxy, pw, browser = [], None, None, None, None
    try:
        proxy = VettingProxy()
        pw = sync_playwright().start()
        browser = pw.chromium.launch(args=ARGS, proxy={"server": f"http://127.0.0.1:{proxy.port}"},
                                     timeout=max(1000, (deadline - time.perf_counter()) * 1000))
        device = check.get("device", "desktop")
        ctx = browser.new_context(viewport=VIEWPORTS[device], user_agent=USER_AGENT[device], is_mobile=device == "mobile",
                                  has_touch=device == "mobile", accept_downloads=False, service_workers="block",
                                  ignore_https_errors=not check.get("verify_tls", True), locale="en-US", timezone_id="UTC")
        page = ctx.new_page()
        seen = {"console": [], "failed": [], "http_errors": []}
        page.on("console", lambda m: m.type == "error" and seen["console"].append(m.text[:300]))
        page.on("pageerror", lambda e: seen["console"].append(str(e)[:300]))
        page.on("requestfailed", lambda r: seen["failed"].append(f"{r.url[:200]} ({r.failure})"))
        page.on("response", lambda r: r.status >= 400 and seen["http_errors"].append(f"{r.status} {r.url[:200]}"))
        for i, step in enumerate(check["steps"]):
            r = _run_step(page, step, values, masked, deadline, seen, proxy)
            if shots == "every_step" or (shots == "failure" and not r["ok"]):
                r["screenshot"] = _screenshot(page, masked)
            steps.append(r)
            if not r["ok"]:
                failure = f"{step['name']}: {r['failure']}"
                break
    except Exception as e:  # noqa: BLE001  the browser itself failed: report it, never raise
        failure = safety.mask(f"browser error: {type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}", masked)
    finally:
        for close in (lambda: browser and browser.close(), lambda: pw and pw.stop(), lambda: proxy and proxy.stop()):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    total = sum(s["timings"].get("total_ms", 0) for s in steps)
    failed = next((i for i, s in enumerate(steps) if not s["ok"]), None)
    if failure and failed is None and steps:
        failed = len(steps) - 1
    return {"ok": failure is None, "failure": failure, "failed_step": failed, "steps": steps, "total_ms": round(total, 1),
            "tls_days": None, "started": started}


def _left_ms(deadline, cap=STEP_TIMEOUT_MS):
    left = (deadline - time.perf_counter()) * 1000
    if left <= 50:
        raise StepFailed("timed out (the check's time limit)")
    return min(left, cap)


def _run_step(page, step, values, masked, deadline, seen, proxy):
    started, t0 = time.time(), time.perf_counter()
    marks = {k: len(v) for k, v in seen.items()}
    blocked_before = len(proxy.blocked)
    a, status, vitals, extracted, failure = step["action"], None, None, [], None
    sel = step.get("selector")
    try:
        timeout = _left_ms(deadline, step.get("timeout_ms") or STEP_TIMEOUT_MS)
        if a == "navigate":
            url = safety.sub(step["url"], values)
            safety.target(url)                        # refuse an internal URL outright (the proxy would too)
            resp = page.goto(url, wait_until="load", timeout=timeout)
            status = resp.status if resp else None
            if status is not None and status >= 400:
                host = (urlsplit(resp.url).hostname or "").lower()
                refused = [b for b in proxy.blocked[blocked_before:] if b["host"] == host]
                raise StepFailed(f"refused: {refused[-1]['reason']}" if refused else f"the page returned {status}")
            vitals = _vitals(page)
        elif a == "click":
            page.click(sel, timeout=timeout)
        elif a == "hover":
            page.hover(sel, timeout=timeout)
        elif a == "type":
            page.fill(sel, safety.sub(step["text"], values), timeout=timeout)
        elif a == "select":
            page.select_option(sel, safety.sub(step["value"], values), timeout=timeout)
        elif a == "press":
            page.press(sel or "body", step["key"], timeout=timeout)
        elif a == "wait_for":
            page.wait_for_selector(sel, state="visible", timeout=timeout)
        elif a == "wait":
            ms = min(step["ms"], _left_ms(deadline, MAX_WAIT_MS))
            page.wait_for_timeout(ms)
        elif a == "assert_text":
            text = safety.sub(step["text"], values)
            where = page.locator(sel).filter(has_text=text) if sel else page.get_by_text(text)
            try:
                where.first.wait_for(state="visible", timeout=timeout)
            except PlaywrightError:
                raise StepFailed(f"text {step['text']!r} not found" + (f" in {sel}" if sel else ""))
        elif a == "assert_no_text":
            text = safety.sub(step["text"], values)
            if page.get_by_text(text).filter(visible=True).count():
                raise StepFailed(f"text {step['text']!r} is on the page")
        elif a == "assert_element":
            try:
                page.wait_for_selector(sel, state="visible", timeout=timeout)
            except PlaywrightError:
                raise StepFailed(f"element {sel} not found")
        elif a == "assert_url":
            want = safety.sub(step["value"], values)
            try:
                page.wait_for_url(lambda u: want in u, timeout=timeout, wait_until="commit")
            except PlaywrightError:
                raise StepFailed(f"URL {safety.mask(page.url, masked)} does not contain {step['value']!r}")
        elif a == "extract":
            el = page.locator(sel).first
            el.wait_for(state="attached", timeout=timeout)
            v = el.get_attribute(step["attribute"], timeout=timeout) if step.get("attribute") else el.inner_text(timeout=timeout)
            v = (v or "").strip()
            if not v:
                raise StepFailed(f"{sel} has no {'attribute ' + step['attribute'] if step.get('attribute') else 'text'}")
            values[step["variable"]] = v
            extracted.append(step["variable"])
            if len(v) >= 4:
                masked.append(v)
        else:
            raise StepFailed(f"unknown action {a!r}")
    except (StepFailed, Refused) as e:
        failure = str(e)
    except PlaywrightError as e:
        failure = _explain(e, proxy.blocked[blocked_before:])
    blocked = proxy.blocked[blocked_before:]
    m = lambda xs: [safety.mask(x, masked) for x in xs[:5]]   # noqa: E731
    return {"name": step["name"], "action": a, "ok": failure is None, "failure": safety.mask(failure, masked) if failure else None,
            "status": status, "url": safety.mask(page.url, masked), "started": started,
            "timings": {"total_ms": round((time.perf_counter() - t0) * 1000, 1)}, "vitals": vitals, "extracted": extracted,
            "console_errors": m(seen["console"][marks["console"]:]), "failed_requests": m(seen["failed"][marks["failed"]:]),
            "http_errors": m(seen["http_errors"][marks["http_errors"]:]),
            "blocked": [b["host"] for b in blocked[:5]], "screenshot": None}


def _explain(e, blocked):
    full = str(e)
    msg = full.splitlines()[0] if full else type(e).__name__
    if "Timeout" in type(e).__name__ or "Timeout " in msg:
        waiting = re.search(r"waiting for (.+)", full)
        return "timed out" + (f" waiting for {waiting[1].strip()[:200]}" if waiting else "")
    if blocked and ("ERR_TUNNEL_CONNECTION_FAILED" in msg or "ERR_PROXY" in msg):
        return f"refused: {blocked[0]['reason']}"
    return msg.replace("Page.", "").replace("Locator.", "")[:300]


def _vitals(page):
    try:
        v = page.evaluate(VITALS_JS)
    except PlaywrightError:
        return None
    return {k: (round(float(x), 4 if k == "cls" else 1) if x is not None else None) for k, x in v.items()}


def _screenshot(page, masked):
    """A JPEG of the viewport, base64, with anything showing a secret painted over."""
    try:
        marked = page.evaluate(MARK_JS, masked) if masked else 0
        img = page.screenshot(type="jpeg", quality=JPEG_QUALITY, timeout=5000, animations="disabled",
                              mask=[page.locator("[data-leasyd-mask]")] if marked else [], mask_color="#1f2937")
        if marked:
            page.evaluate(UNMARK_JS)
        return base64.b64encode(img).decode()
    except PlaywrightError:
        return None


# ------------------------------------------------------------------ Lambda

def handler(event, context):
    """{"check": settings, "secrets": {name: value}, "budget_ms": n, "screenshots": mode} -> result."""
    try:
        return run_browser(event["check"], event.get("secrets") or {}, (event.get("budget_ms") or 0) / 1000 or None,
                           event.get("screenshots"))
    finally:
        if ON_LAMBDA:
            _clean_up()


def _clean_up():
    """Leave nothing for the next run in this environment: other processes, files in /tmp."""
    keep, pid = set(), os.getpid()
    while pid > 1:                                   # this process and its ancestors
        keep.add(pid)
        try:
            pid = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    keep.add(1)
    for p in os.listdir("/proc"):
        if p.isdigit() and int(p) not in keep:
            try:
                os.kill(int(p), signal.SIGKILL)
            except OSError:
                pass
    for name in os.listdir("/tmp"):
        path = os.path.join("/tmp", name)
        try:
            shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)
        except OSError:
            pass


__all__ = ["run_browser", "handler", "VettingProxy", "MASK"]
