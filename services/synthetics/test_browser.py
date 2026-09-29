"""Browser checks against a local site, with the real Chromium and the vetting proxy.

The site is on 127.0.0.1, which the tests allow (and nothing else inside): a second server on
127.0.0.2 stands for "inside" and must never be reached.
"""
import base64
import http.server
import ipaddress
import os
import ssl
import subprocess
import sys
import threading
from urllib.parse import parse_qs

import pytest

sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("playwright")

import browser  # noqa: E402
import safety  # noqa: E402

REAL_PUBLIC = safety.public
PASSWORD, TOKEN = "hunter2-very-secret", "tok_4f9a8b7c6d5e"
INSIDE_HITS = []


class Site(http.server.BaseHTTPRequestHandler):
    inside = ""   # http://127.0.0.2:<port>

    def _page(self, body, status=200, headers=()):
        data = f"<!doctype html><html><head><title>Acme</title></head><body>{body}</body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self._page('<h1>Welcome to Acme</h1><a id="login" href="/login">Log in</a>')
        if p == "/login":
            return self._page('<form method="post" action="/session"><input id="email" name="email">'
                              '<input id="password" name="password" type="password">'
                              '<select id="plan" name="plan"><option value="free">Free</option><option value="pro">Pro</option></select>'
                              '<button id="go" type="submit">Sign in</button></form>')
        if p == "/account":
            return self._page(f'<h1>Hello Ana</h1><span id="token">{TOKEN}</span><a id="orders" data-id="order-8812" href="/orders">Orders</a>')
        if p == "/orders":
            return self._page('<ul><li>Order 8812 - shipped</li></ul>')
        if p == "/echo":
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            return self._page(f'<p id="echo">You typed: {q.get("v", [""])[0]}</p><input id="field" value="{q.get("v", [""])[0]}">')
        if p == "/sneaky":   # tries to reach inside in every way a page can
            return self._page(f'<h1>Sneaky</h1><img src="{self.inside}/img"><script src="{self.inside}/js"></script>'
                              f'<iframe src="{self.inside}/frame"></iframe>'
                              f'<script>fetch("{self.inside}/fetch").catch(()=>{{}});'
                              'fetch("http://169.254.169.254/latest/meta-data/").catch(()=>{});'
                              f'try {{ new WebSocket("{self.inside.replace("http", "ws")}/ws"); }} catch (e) {{}}</script>')
        if p == "/to-inside":
            self.send_response(302)
            self.send_header("Location", f"{self.inside}/admin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/broken":
            return self._page("oops", status=500)
        if p == "/errors":
            return self._page('<p>ok</p><script>console.error("boom"); undefinedFn();</script><img src="/missing.png">')
        self._page("not found", status=404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode())
        if form.get("password") == [PASSWORD] and form.get("plan") == ["pro"]:
            self.send_response(303)
            self.send_header("Location", "/account")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._page("<p>Invalid password</p>", status=200)

    def log_message(self, *a):
        pass


class Inside(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        INSIDE_HITS.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "6")
        self.end_headers()
        self.wfile.write(b"secret")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def servers():
    inside = http.server.ThreadingHTTPServer(("127.0.0.2", 0), Inside)
    threading.Thread(target=inside.serve_forever, daemon=True).start()
    Site.inside = f"http://127.0.0.2:{inside.server_address[1]}"
    site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{site.server_address[1]}", Site.inside
    site.shutdown()
    inside.shutdown()


@pytest.fixture(autouse=True)
def only_the_site_is_public(monkeypatch):
    # Exactly 127.0.0.1 counts as public here; 127.0.0.2 and everything else inside stays refused.
    monkeypatch.setattr(safety, "public", lambda ip: ip == ipaddress.ip_address("127.0.0.1") or REAL_PUBLIC(ip))
    INSIDE_HITS.clear()


def check(steps, **kw):
    return {"type": "browser", "name": "t", "frequency": 5, "timeout_ms": kw.pop("timeout_ms", 30000), "variables": kw.pop("variables", {}),
            "device": "desktop", "screenshots": kw.pop("screenshots", "failure"), "verify_tls": True,
            "steps": [{"name": s.get("name", f"s{i}"), **s} for i, s in enumerate(steps)], **kw}


def test_login_journey(servers):
    base, _ = servers
    r = browser.run_browser(check([
        {"action": "navigate", "url": "{base}/"},
        {"action": "assert_text", "text": "Welcome to Acme"},
        {"action": "click", "selector": "#login"},
        {"action": "type", "selector": "#email", "text": "ana@acme.test"},
        {"action": "type", "selector": "#password", "text": "{password}"},
        {"action": "select", "selector": "#plan", "value": "Pro"},
        {"action": "click", "selector": "text=Sign in"},
        {"action": "assert_url", "value": "/account"},
        {"action": "extract", "selector": "#token", "variable": "token"},
        {"action": "extract", "selector": "#orders", "variable": "order", "attribute": "data-id"},
        {"action": "assert_element", "selector": "#orders"},
    ], variables={"base": base}), {"password": PASSWORD})
    assert r["ok"], r["failure"]
    assert len(r["steps"]) == 11 and r["failed_step"] is None
    nav = r["steps"][0]
    assert nav["status"] == 200 and nav["vitals"]["load_ms"] is not None and nav["vitals"]["ttfb_ms"] is not None
    assert r["steps"][8]["extracted"] == ["token"]
    assert not any(s["screenshot"] for s in r["steps"])          # failures only
    assert PASSWORD not in str(r) and TOKEN not in str(r)        # typed and extracted, never shown


def test_failure_has_a_screenshot_and_reason(servers):
    base, _ = servers
    r = browser.run_browser(check([{"action": "navigate", "url": base + "/login"},
                                   {"action": "type", "selector": "#password", "text": "wrong"},
                                   {"action": "click", "selector": "#go"},
                                   {"action": "assert_text", "text": "Hello Ana", "timeout_ms": 1500}]), {})
    assert not r["ok"] and r["failed_step"] == 3
    assert "'Hello Ana' not found" in r["failure"]
    img = base64.b64decode(r["steps"][3]["screenshot"])
    assert img[:3] == b"\xff\xd8\xff"                            # a JPEG


def test_every_step_screenshots_and_error_page(servers):
    base, _ = servers
    r = browser.run_browser(check([{"action": "navigate", "url": base + "/"}, {"action": "navigate", "url": base + "/broken"}],
                                  screenshots="every_step"), {})
    assert [bool(s["screenshot"]) for s in r["steps"]] == [True, True]
    assert r["failure"].endswith("the page returned 500")


def test_console_errors_and_failed_requests_are_reported(servers):
    base, _ = servers
    s = browser.run_browser(check([{"action": "navigate", "url": base + "/errors"}]), {})["steps"][0]
    assert any("boom" in c for c in s["console_errors"]) and any("undefinedFn" in c for c in s["console_errors"])
    assert any(e.startswith("404 ") and "missing.png" in e for e in s["http_errors"])


def test_nothing_inside_is_reachable_from_a_page(servers):
    base, inside = servers
    r = browser.run_browser(check([{"action": "navigate", "url": base + "/sneaky"}, {"action": "wait", "ms": 800}]), {})
    assert r["ok"], r["failure"]                                  # the page itself loads...
    assert INSIDE_HITS == []                                      # ...but nothing it asked for inside was reached
    assert "127.0.0.2" in r["steps"][0]["blocked"] + r["steps"][1]["blocked"]


@pytest.mark.parametrize("url", ["http://127.0.0.2:{port}/", "http://169.254.169.254/latest/meta-data/", "http://localhost:{port}/",
                                 "http://[::1]:{port}/", "{base}/to-inside"])
def test_navigating_inside_is_refused(servers, url):
    base, inside = servers
    port = inside.rsplit(":", 1)[1]
    r = browser.run_browser(check([{"action": "navigate", "url": url.format(base=base, port=port)}], timeout_ms=8000), {})
    assert not r["ok"]
    assert "public address" in r["failure"] or "non-public" in r["failure"], r["failure"]
    assert INSIDE_HITS == []


def test_secrets_are_masked_in_screenshots_and_urls(servers):
    base, _ = servers
    r = browser.run_browser(check([{"action": "navigate", "url": base + "/echo?v={password}"},
                                   {"action": "assert_text", "text": "not there", "timeout_ms": 500}]), {"password": PASSWORD})
    assert PASSWORD not in str(r)
    assert "v=••••" in r["steps"][0]["url"]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:                                   # the screenshot marks both the text and the field
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content(f'<p>You typed: {PASSWORD}</p><input value="{PASSWORD}"><p>other</p>')
        assert page.evaluate(browser.MARK_JS, [PASSWORD]) == 2
        b.close()


def test_time_limit(servers):
    base, _ = servers
    r = browser.run_browser(check([{"action": "navigate", "url": base + "/"}, {"action": "wait_for", "selector": "#never"}],
                                  timeout_ms=2500), {})
    assert not r["ok"] and "timed out" in r["failure"]
    assert sum(s["timings"]["total_ms"] for s in r["steps"]) < 4000


def test_https_through_the_proxy(tmp_path, servers):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "2",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{srv.server_address[1]}/"
    bad = browser.run_browser(check([{"action": "navigate", "url": url}], timeout_ms=8000), {})
    assert not bad["ok"] and "CERT" in bad["failure"]              # self-signed: refused while TLS is verified
    ok = browser.run_browser(check([{"action": "navigate", "url": url}, {"action": "assert_text", "text": "Welcome"}],
                                   verify_tls=False), {})
    assert ok["ok"], ok["failure"]
    srv.shutdown()


def test_control_the_sneaky_page_does_reach_inside_when_allowed(servers, monkeypatch):
    # Proves the test above can fail: allow 127.0.0.2 too and the same page reaches it.
    base, _ = servers
    monkeypatch.setattr(safety, "public", lambda ip: ip.is_loopback)
    r = browser.run_browser(check([{"action": "navigate", "url": base + "/sneaky"}, {"action": "wait", "ms": 800}]), {})
    assert r["ok"]
    assert {"/img", "/js", "/frame", "/fetch"} <= set(INSIDE_HITS)


def test_end_to_end_payload_and_telemetry(servers, tmp_path, monkeypatch):
    """Settings as the API validates them -> the browser function's JSON payload -> telemetry the pipeline accepts."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "compaction"))
    import gzip
    import json
    import compact
    import duckdb
    import ingest
    import synthetics
    base, _ = servers
    settings = synthetics.validate({"type": "browser", "name": "Shop login", "frequency": 5, "secret_names": ["password"],
                                    "variables": {"base": base}, "screenshots": "every_step", "steps": [
        {"name": "Open login", "action": "navigate", "url": "{base}/login"},
        {"name": "Password", "action": "type", "selector": "#password", "text": "{password}"},
        {"name": "Plan", "action": "select", "selector": "#plan", "value": "pro"},
        {"name": "Sign in", "action": "press", "selector": "#password", "key": "Enter"},
        {"name": "Signed in", "action": "assert_text", "text": "Hello Ana"}]})
    monkeypatch.setattr(synthetics, "_lambda", type("L", (), {"invoke": staticmethod(lambda **kw: {
        "Payload": type("P", (), {"read": lambda self: json.dumps(browser.handler(json.loads(kw["Payload"]), None)).encode()})()})})())
    r = synthetics.run_any({**settings, "pk": "check#acme#abc", "tenant": "acme", "secrets": {"password": "ciphertext"}},
                           {"password": PASSWORD})
    assert r["ok"], r["failure"]
    assert all(s["screenshot"] for s in r["steps"]) and PASSWORD not in json.dumps(r)
    r = synthetics.store_screenshots("acme", "abc123abc123", r)          # no bucket here: nothing saved
    for signal, doc in synthetics.telemetry("abc123abc123", settings, r).items():
        ingest.parse(signal, json.dumps(doc).encode(), "application/json")
        f = tmp_path / f"{signal}.json.gz"
        f.write_bytes(gzip.compress(b"".join(ingest.to_records(signal, doc))))
        assert compact.compact(signal, [str(f)], str(tmp_path / signal), "b1", "2026-09-21", "12")
    spans = duckdb.sql(f"SELECT name, attributes['step.action'], attributes['check.type'] FROM '{tmp_path}/traces/**/*.parquet' "
                       "ORDER BY ts_unix_nano, name").fetchall()
    assert [s[0] for s in spans] == ["Shop login", "Open login", "Password", "Plan", "Sign in", "Signed in"]
    assert spans[1][1] == "navigate" and spans[0][2] == "browser"
    names = {n for (n,) in duckdb.sql(f"SELECT DISTINCT metric_name FROM '{tmp_path}/metrics/**/*.parquet'").fetchall()}
    assert {"synthetics.browser.lcp", "synthetics.browser.load", "synthetics.browser.ttfb", "synthetics.check.success"} <= names
