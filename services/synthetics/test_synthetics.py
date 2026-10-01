import base64
import gzip
import http.server
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time

import boto3
import pytest
from moto import mock_aws

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))
sys.path.insert(0, os.path.join(HERE, "..", "compaction"))
os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing")
os.environ.pop("AWS_SESSION_TOKEN", None)

import synthetics  # noqa: E402

REAL_PUBLIC = synthetics.safety.public
PASSWORD, TOKEN = "hunter2-very-secret", "tok_4f9a8b7c6d5e"


# ------------------------------------------------------------------ a local site to check

class Site(http.server.BaseHTTPRequestHandler):
    def _send(self, status, body=b"", headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/login":
            auth = self.headers.get("Authorization", "")
            ok = auth == "Basic " + base64.b64encode(f"ana:{PASSWORD}".encode()).decode() and body.get("remember") is True
            if not ok:
                return self._send(401, b'{"error": "bad credentials"}')
            return self._send(200, json.dumps({"token": TOKEN, "user": {"id": 42, "name": "Ana"}}).encode(),
                              [("Content-Type", "application/json"), ("Set-Cookie", "session=s-777; Path=/; HttpOnly"),
                               ("X-Request-Id", "req-9")])
        self._send(404)

    def do_GET(self):
        if self.path.startswith("/orders/"):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}" or "session=s-777" not in self.headers.get("Cookie", ""):
                return self._send(403, f'{{"error": "forbidden", "echo": "{self.headers.get("Authorization")}"}}'.encode())
            oid = self.path.rsplit("/", 1)[1]
            return self._send(200, json.dumps({"order": {"id": int(oid), "total": 19.5, "status": "shipped", "items": [{"sku": "A1"}]}}).encode(),
                              [("Content-Type", "application/json")])
        if self.path == "/slow":
            time.sleep(1.5)
        if self.path == "/to-inside":
            return self._send(302, headers=[("Location", "http://10.0.0.8/admin")])
        if self.path == "/to-ok":
            return self._send(301, headers=[("Location", "/ok")])
        if self.path == "/aaa":
            return self._send(200, b"a" * 5000 + b"!")
        if self.path == "/broken":
            return self._send(500, b"oops")
        self._send(200, b"Welcome to Acme")

    def log_message(self, *a):
        pass


@pytest.fixture
def site(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # The local test site is on loopback: allow exactly that; everything else stays checked for real.
    monkeypatch.setattr(synthetics.safety, "public", lambda ip: ip.is_loopback or REAL_PUBLIC(ip))
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def one_step(url, **step):
    return synthetics.validate({"name": "t", "timeout_ms": 5000, "steps": [{"url": url, **step}]})


def login_flow(site, **extra):
    return synthetics.validate({
        "name": "Order lookup", "timeout_ms": 10000, "variables": {"base": site}, "secret_names": ["password"], **extra,
        "steps": [
            {"name": "Log in", "method": "POST", "url": "{base}/login", "body": '{"remember": true}',
             "headers": {"Content-Type": "application/json"}, "auth": {"type": "basic", "username": "ana", "password": "{password}"},
             "extract": [{"name": "token", "from": "json", "expr": "token"}, {"name": "user_id", "from": "json", "expr": "$.user.id"},
                         {"name": "request_id", "from": "header", "expr": "X-Request-Id"},
                         {"name": "first_name", "from": "regex", "expr": r'"name":\s*"(\w+)"'}],
             "constraints": [{"type": "status", "expr": "200"}, {"type": "json", "path": "user.name", "op": "equals", "value": "Ana"}]},
            {"name": "Get order", "url": "{base}/orders/{user_id}", "auth": {"type": "bearer", "token": "{token}"},
             "constraints": [{"type": "status", "expr": "2xx"}, {"type": "json", "path": "order.status", "op": "equals", "value": "shipped"},
                             {"type": "json", "path": "order.total", "op": "gt", "value": "10"},
                             {"type": "json", "path": "order.id", "op": "equals", "value": "{user_id}"},
                             {"type": "json", "path": "order.items[0].sku", "op": "exists"},
                             {"type": "header", "name": "Content-Type", "op": "contains", "value": "json"},
                             {"type": "body_not_contains", "value": "error"}, {"type": "max_ms", "value": 5000}]},
        ]})


# ------------------------------------------------------------------ never reach inside

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.1.2.3/", "http://192.168.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://[::1]/", "http://[fd00::1]/", "http://[::ffff:10.0.0.1]/", "http://0.0.0.0/",
    "http://localhost:9001/2018-06-01/runtime/invocation/next", "http://metadata.internal/", "ftp://example.com/",
    "http://user:pass@example.com/", "file:///etc/passwd", "http:///nohost",
])
def test_non_public_or_odd_urls_are_refused(url):
    with pytest.raises(synthetics.Refused):
        one_step(url)


def test_a_name_that_resolves_inside_is_refused(monkeypatch):
    def fake(host, port, **kw):
        ips = {"good.example": ["93.184.216.34"], "sneaky.example": ["93.184.216.34", "10.0.0.5"]}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert synthetics.resolve("good.example", 443) == ["93.184.216.34"]
    r = synthetics.run_check(one_step("https://sneaky.example/"), {})
    assert not r["ok"] and "non-public" in r["failure"]


def test_a_variable_cant_smuggle_an_internal_host():
    c = synthetics.validate({"name": "t", "variables": {"host": "169.254.169.254"}, "steps": [{"url": "http://{host}/latest/"}]})
    r = synthetics.run_check(c, {})
    assert not r["ok"] and "public address" in r["failure"]


def test_public_means_globally_routable():
    assert REAL_PUBLIC(ipaddress.ip_address("93.184.216.34"))
    for ip in ("10.0.0.1", "172.16.0.1", "169.254.169.254", "127.0.0.1", "100.64.0.1", "224.0.0.1", "::1", "fe80::1"):
        assert not REAL_PUBLIC(ipaddress.ip_address(ip)), ip


# ------------------------------------------------------------------ settings

@pytest.mark.parametrize("expr,status,ok", [
    ("<400", 200, True), ("<400", 404, False), ("2xx", 204, True), ("3xx, 404, 406-410, >=500", 408, True),
    ("3xx, 404, 406-410, >=500", 405, False), (">=500", 503, True), ("!=200", 200, False), ("200,204", 204, True),
])
def test_status_expressions(expr, status, ok):
    assert synthetics.status_matches(expr, status) is ok


def test_json_paths():
    doc = {"data": {"items": [{"id": 7, "full name": "x"}]}, "token": "t"}
    assert synthetics.json_get(doc, "data.items[0].id") == (True, 7)
    assert synthetics.json_get(doc, "$.token") == (True, "t")
    assert synthetics.json_get(doc, "data.items[0]['full name']") == (True, "x")
    assert synthetics.json_get(doc, "data.items[3].id") == (False, None)
    with pytest.raises(synthetics.Refused):
        synthetics.json_path("data..[x")


def test_settings_are_validated():
    ok = {"name": "t", "steps": [{"url": "https://example.com/"}]}
    for bad in ({"frequency": 2}, {"timeout_ms": 60_000}, {"steps": []}, {"steps": [{"url": "https://example.com/"}] * 11},
                {"steps": [{"url": "https://example.com/", "method": "TRACE"}]},
                {"steps": [{"url": "https://example.com/{nope}"}]},                                      # unknown variable
                {"steps": [{"url": "https://example.com/", "headers": {"Host": "evil"}}]},
                {"steps": [{"url": "https://example.com/", "auth": {"type": "bearer", "token": "plain-token"}}]},   # not a secret
                {"steps": [{"url": "https://example.com/", "constraints": [{"type": "status", "expr": "2x"}]}]},
                {"steps": [{"url": "https://example.com/", "constraints": [{"type": "body_regex", "value": "("}]}]},
                {"steps": [{"url": "https://example.com/", "extract": [{"name": "1x", "from": "json", "expr": "a"}]}]},
                {"steps": [{"url": "https://example.com/{token}"}, {"url": "https://example.com/", "extract": [{"name": "token", "from": "json", "expr": "t"}]}]}):
        with pytest.raises(synthetics.Refused):
            synthetics.validate({**ok, **bad})
    c = synthetics.validate(ok)
    assert c["steps"][0]["constraints"] == [{"type": "status", "expr": "<400"}] and c["steps"][0]["verify_tls"]


# ------------------------------------------------------------------ running steps against a real server

def test_multi_step_login_flow_with_variables_auth_and_cookies(site):
    r = synthetics.run_check(login_flow(site), {"password": PASSWORD})
    assert r["ok"], r["failure"]
    assert [s["name"] for s in r["steps"]] == ["Log in", "Get order"]
    assert r["steps"][0]["extracted"] == ["first_name", "request_id", "token", "user_id"]
    assert r["steps"][1]["url"].endswith("/orders/42")
    assert all(0 <= s["timings"]["total_ms"] < 2000 for s in r["steps"]) and r["total_ms"] > 0


def test_the_first_failing_step_ends_the_run_and_nothing_secret_is_recorded(site):
    c = login_flow(site)
    c["steps"][1]["auth"] = {"type": "none"}                         # the order call now gets 403, echoing a header
    c["steps"][1]["headers"] = {"Authorization": "Bearer {token}X"}
    r = synthetics.run_check(c, {"password": PASSWORD})
    assert not r["ok"] and r["failed_step"] == 1 and r["failure"].startswith("Get order: status 403")
    sample = r["steps"][1]["body_sample"]
    assert "forbidden" in sample and TOKEN not in sample and "••••" in sample
    wrong = synthetics.run_check(login_flow(site), {"password": "nope"})
    assert wrong["failed_step"] == 0 and "status 401" in wrong["failure"] and len(wrong["steps"]) == 1
    assert PASSWORD not in json.dumps(r) and PASSWORD not in json.dumps(wrong)


def test_each_step_records_its_request_and_response_without_credentials(site):
    r = synthetics.run_check(login_flow(site), {"password": PASSWORD})
    login, order = r["steps"]
    assert login["request"]["method"] == "POST" and login["request"]["body"] == '{"remember": true}'
    assert login["request"]["headers"]["Authorization"] == "••••" and login["request"]["headers"]["Content-Type"] == "application/json"
    assert login["response"]["headers"]["set-cookie"] == "••••" and login["response"]["headers"]["content-type"] == "application/json"
    assert '"name": "Ana"' in login["response"]["body"] and TOKEN not in login["response"]["body"]
    assert order["request"]["headers"]["Authorization"] == "••••" and '"shipped"' in order["response"]["body"]
    assert PASSWORD not in json.dumps(r) and TOKEN not in json.dumps(r) and "s-777" not in json.dumps(r)
    attrs = {a["key"]: a["value"] for a in synthetics.telemetry("abc123abc123", login_flow(site), r)["traces"][
        "resourceSpans"][0]["scopeSpans"][0]["spans"][1]["attributes"]}
    assert json.loads(attrs["http.request.headers"]["stringValue"])["Authorization"] == "••••"
    assert attrs["http.request.method"]["stringValue"] == "POST" and "Ana" in attrs["http.response.body"]["stringValue"]
    # Bodies only when the step records them; headers always. A refused redirect shows where it pointed.
    quiet = synthetics.run_check(one_step(site + "/ok", record_body=False), {})
    assert quiet["steps"][0]["response"]["body"] is None and quiet["steps"][0]["response"]["headers"]
    refused = synthetics.run_check(one_step(site + "/to-inside"), {})
    assert refused["steps"][0]["response"]["headers"]["location"] == "http://10.0.0.8/admin" and not refused["ok"]


@pytest.mark.parametrize("path,constraints,why", [
    ("/broken", [], "status 500, expected <400"),
    ("/ok", [{"type": "body_contains", "value": "Goodbye"}], "does not contain 'Goodbye'"),
    ("/ok", [{"type": "body_regex", "value": "^Welcome to \\d+$"}], "does not match"),
    ("/ok", [{"type": "json", "path": "a", "op": "exists"}], "not JSON"),
    ("/ok", [{"type": "header", "name": "X-Missing", "op": "exists"}], "header X-Missing is None"),
])
def test_constraints_fail_with_a_reason(site, path, constraints, why):
    r = synthetics.run_check(one_step(site + path, constraints=constraints), {})
    assert not r["ok"] and why in r["failure"], r["failure"]


def test_timeouts_redirects_and_regex_limits(site):
    slow = synthetics.validate({"name": "t", "timeout_ms": 1000, "steps": [{"url": site + "/slow"}]})
    assert synthetics.run_check(slow, {})["failure"] == "Step 1: timed out"
    assert synthetics.run_check(one_step(site + "/to-ok"), {})["ok"]
    assert synthetics.run_check(one_step(site + "/to-ok", follow_redirects=False, constraints=[{"type": "status", "expr": "301"}]), {})["ok"]
    r = synthetics.run_check(one_step(site + "/to-inside"), {})
    assert not r["ok"] and "public address" in r["failure"]
    t0 = time.perf_counter()                                          # a catastrophic regex is stopped, not left to run
    r = synthetics.run_check(one_step(site + "/aaa", constraints=[{"type": "body_regex", "value": "(a|aa)+$"}]), {})
    assert not r["ok"] and "took too long" in r["failure"] and time.perf_counter() - t0 < 3


def test_a_variable_cant_inject_headers(site):
    c = synthetics.validate({"name": "t", "variables": {"v": "x"}, "steps": [{"url": site + "/ok", "headers": {"X-A": "{v}"}}]})
    c["variables"]["v"] = "x\r\nX-Evil: 1"
    r = synthetics.run_check(c, {})
    assert not r["ok"] and "line break" in r["failure"]


# ------------------------------------------------------------------ API, secrets, per tenant

@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "tenant", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "by-tenant", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "tenant", "KeyType": "HASH"},
                                                   {"AttributeName": "pk", "KeyType": "RANGE"}]}])
        key = boto3.client("kms").create_key()["KeyMetadata"]["KeyId"]
        monkeypatch.setattr(synthetics, "KMS_KEY", key)
        monkeypatch.setattr(synthetics, "_table", None)
        monkeypatch.setattr(synthetics, "_kms", None)
        sent = []
        monkeypatch.setattr(synthetics.ingest, "put_records", lambda stream, records: sent.append((stream, records)))
        yield sent


def call(tenant, method, resource, body=None, check_id=None):
    event = {"httpMethod": method, "resource": resource, "pathParameters": {"id": check_id} if check_id else None,
             # as API Gateway delivers it: every body is binary (BinaryMediaTypes */*), so base64
             "body": base64.b64encode(json.dumps(body).encode()).decode() if body is not None else None,
             "isBase64Encoded": body is not None,
             "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": f"ana@{tenant}.io"}}}}
    r = synthetics.api(event, None)
    return r["statusCode"], json.loads(r["body"])


def settings(url="https://example.com/", **kw):
    return {"name": "Home", "steps": [{"url": url}], **kw}


def test_crud_and_each_tenant_sees_only_its_own_checks(aws):
    s, a = call("acme", "POST", "/v1/app/checks", {**settings(), "tenant": "globex"})
    assert s == 201 and a["name"] == "Home" and "tenant" not in a
    call("globex", "POST", "/v1/app/checks", settings("https://example.org/", name="Theirs"))
    assert [c["name"] for c in call("acme", "GET", "/v1/app/checks")[1]["checks"]] == ["Home"]
    for method, resource in (("GET", "/v1/app/checks/{id}"), ("PUT", "/v1/app/checks/{id}"),
                             ("DELETE", "/v1/app/checks/{id}"), ("POST", "/v1/app/checks/{id}/run")):
        assert call("globex", method, resource, {}, a["id"])[0] == 404        # someone else's check
    s, u = call("acme", "PUT", "/v1/app/checks/{id}", {"frequency": 1, "enabled": False}, a["id"])
    assert s == 200 and u["frequency"] == 1 and not u["enabled"] and u["steps"][0]["url"] == "https://example.com/"
    assert call("acme", "PUT", "/v1/app/checks/{id}", settings("http://10.0.0.1/"), a["id"])[0] == 400
    assert call("acme", "DELETE", "/v1/app/checks/{id}", None, a["id"])[0] == 200
    assert call("acme", "GET", "/v1/app/checks")[1]["checks"] == []
    ev = {"httpMethod": "GET", "resource": "/v1/app/checks", "requestContext": {"authorizer": {"claims": {}}}}
    assert synthetics.api(ev, None)["statusCode"] == 401


def test_secrets_are_encrypted_write_only_and_bound_to_their_check(aws, site):
    body = {**login_flow(site), "secrets": {"password": PASSWORD}}
    s, a = call("acme", "POST", "/v1/app/checks", body)
    assert s == 201 and a["secret_names"] == ["password"] and PASSWORD not in json.dumps(a)
    stored = boto3.resource("dynamodb").Table("obs-tenants").get_item(Key={"pk": f"check#acme#{a['id']}"})["Item"]
    assert PASSWORD not in json.dumps(stored, default=str)                       # only ciphertext at rest
    s, r = call("acme", "POST", "/v1/app/checks/{id}/run", None, a["id"])
    assert s == 200 and r["result"]["ok"], r
    assert PASSWORD not in json.dumps(aws, default=str) and TOKEN not in json.dumps(aws, default=str)
    # Editing without retyping keeps the secret; "Test" of edited settings uses the saved one.
    assert call("acme", "PUT", "/v1/app/checks/{id}", {"name": "Renamed"}, a["id"])[1]["secret_names"] == ["password"]
    s, t = call("acme", "POST", "/v1/app/checks/test", {**login_flow(site), "id": a["id"]})
    assert s == 200 and t["result"]["ok"]
    # A ciphertext copied to another check (or tenant) doesn't decrypt there.
    with pytest.raises(Exception):
        synthetics.decrypt_secrets("globex", a["id"], stored["secrets"])
    with pytest.raises(Exception):
        synthetics.decrypt_secrets("acme", "000000000000", stored["secrets"])
    # Removing a secret the check still uses is refused; replacing it works.
    assert call("acme", "PUT", "/v1/app/checks/{id}", {"secrets": {"password": None}}, a["id"])[0] == 400
    assert call("acme", "PUT", "/v1/app/checks/{id}", {"secrets": {"password": "other"}}, a["id"])[0] == 200
    assert not call("acme", "POST", "/v1/app/checks/{id}/run", None, a["id"])[1]["result"]["ok"]


def test_limit_and_bad_input(aws, monkeypatch):
    monkeypatch.setattr(synthetics, "MAX_CHECKS", 2)
    for i in range(2):
        assert call("acme", "POST", "/v1/app/checks", settings(name=f"c{i}"))[0] == 201
    s, e = call("acme", "POST", "/v1/app/checks", settings(name="c3"))
    assert s == 400 and "at most 2" in e["error"]
    assert call("acme", "POST", "/v1/app/checks", settings("http://169.254.169.254/"))[0] == 400


# ------------------------------------------------------------------ schedule and results

def test_due_spreads_checks_over_their_period():
    ids = [f"{i:012x}" for i in range(0, 3000, 7)]
    for f in (1, 5, 15):
        assert {sum(synthetics.due(c, f, m) for m in range(60)) for c in ids} == {60 // f}
    per_minute = [sum(synthetics.due(c, 15, m) for c in ids) for m in range(15)]
    assert max(per_minute) - min(per_minute) <= 2


def test_tick_hands_due_checks_to_the_runner(aws, monkeypatch):
    monkeypatch.setattr(synthetics, "MAX_CHECKS", 50)
    for i in range(30):
        call("acme", "POST", "/v1/app/checks", settings(name=f"c{i}", frequency=1))
    call("acme", "POST", "/v1/app/checks", settings(name="off", frequency=1, enabled=False))
    invoked = []
    monkeypatch.setattr(synthetics.boto3, "client", lambda name: type("L", (), {
        "invoke": lambda self, **kw: invoked.append(json.loads(kw["Payload"]))})())
    assert synthetics.tick({}, None) == {"due": 30}
    assert [len(p["checks"]) for p in invoked] == [25, 5]


def test_runner_records_each_check_for_its_tenant(aws, site):
    _, a = call("acme", "POST", "/v1/app/checks", {**login_flow(site), "secrets": {"password": PASSWORD}})
    item = boto3.resource("dynamodb").Table("obs-tenants").get_item(Key={"pk": f"check#acme#{a['id']}"})["Item"]
    out = synthetics.run({"checks": [synthetics._plain(item)]}, None)
    assert out["ran"][0]["ok"] and {s for s, _ in aws} == {"obs-t-acme-traces", "obs-t-acme-metrics"}


def test_results_are_valid_telemetry_the_pipeline_accepts(tmp_path, site):
    import compact
    import duckdb
    import ingest
    c = login_flow(site)
    ok = synthetics.run_check(c, {"password": PASSWORD})
    bad = synthetics.run_check(c, {"password": "nope"})
    for name, result in (("ok", ok), ("bad", bad)):
        for signal, doc in synthetics.telemetry("abc123abc123", c, result).items():
            ingest.parse(signal, json.dumps(doc).encode(), "application/json")
            f = tmp_path / f"{name}-{signal}.json.gz"
            f.write_bytes(gzip.compress(b"".join(ingest.to_records(signal, doc))))
            assert compact.compact(signal, [str(f)], str(tmp_path / name / signal), "b1", "2026-09-21", "12")
    spans = duckdb.sql(f"SELECT name, status_code, attributes['step.index'], attributes['check.result'] "
                       f"FROM '{tmp_path}/ok/traces/**/*.parquet' ORDER BY ts_unix_nano, name").fetchall()
    assert [s[0] for s in spans] == ["Order lookup", "Log in", "Get order"] and spans[0][3] == "pass"
    names = duckdb.sql(f"SELECT DISTINCT metric_name FROM '{tmp_path}/ok/metrics/**/*.parquet' ORDER BY 1").fetchall()
    assert [n[0] for n in names] == ["synthetics.check.duration", "synthetics.check.success", "synthetics.step.duration"]
    (log,) = duckdb.sql(f"SELECT body FROM '{tmp_path}/bad/logs/**/*.parquet'").fetchall()
    assert log[0].startswith("Check 'Order lookup' failed at Log in: status 401") and "bad credentials" in log[0]
    assert "logs" not in synthetics.telemetry("abc123abc123", c, ok)


# ------------------------------------------------------------------ browser checks (the browser itself: test_browser.py)

def browser_settings(**kw):
    return {"type": "browser", "name": "Shop", "frequency": 5, "steps": [
        {"action": "navigate", "url": "https://shop.example.com/login"},
        {"action": "type", "selector": "#email", "text": "ana@acme.io"},
        {"action": "type", "selector": "#password", "text": "{password}"},
        {"action": "press", "key": "Enter", "selector": "#password"},
        {"action": "extract", "selector": "#order", "variable": "order"},
        {"action": "navigate", "url": "https://shop.example.com/orders/{order}"},
        {"action": "assert_text", "text": "Shipped"}], **kw}


JPEG = base64.b64encode(b"\xff\xd8\xff fake jpeg").decode()


def fake_browser(monkeypatch, ok=False):
    calls = []

    def invoke(**kw):
        payload = json.loads(kw["Payload"])
        calls.append(payload)
        step = {"name": "Log in", "action": "navigate", "ok": ok, "failure": None if ok else "text 'Shipped' not found",
                "status": 200, "url": "https://shop.example.com/", "started": time.time(), "timings": {"total_ms": 812.5},
                "vitals": {"ttfb_ms": 80.0, "fcp_ms": 300.0, "lcp_ms": 950.0, "cls": 0.02, "load_ms": 700.0}, "extracted": [],
                "console_errors": ["TypeError: x is undefined"], "failed_requests": [], "http_errors": ["404 https://shop.example.com/a.png"],
                "blocked": ["10.0.0.9"], "screenshot": JPEG}
        out = {"ok": ok, "failure": None if ok else "Log in: text 'Shipped' not found", "failed_step": None if ok else 0,
               "steps": [step], "total_ms": 812.5, "tls_days": None, "started": time.time()}
        return {"Payload": type("P", (), {"read": lambda self: json.dumps(out).encode()})()}
    monkeypatch.setattr(synthetics, "_lambda", type("L", (), {"invoke": staticmethod(invoke)})())
    return calls


def test_frequencies():
    for f in (1, 5, 15, 30, 45, 60):
        assert synthetics.validate(settings(frequency=f))["frequency"] == f
    with pytest.raises(synthetics.Refused, match="1, 5, 15, 30, 45 or 60"):
        synthetics.validate(settings(frequency=10))
    assert synthetics.due("000000000000", 45, 90) and not synthetics.due("000000000000", 45, 60)


def test_browser_validation():
    c = synthetics.validate({**browser_settings(), "secret_names": ["password"]})
    assert c["type"] == "browser" and c["device"] == "desktop" and c["screenshots"] == "failure" and c["timeout_ms"] == 30000
    assert c["steps"][4] == {"name": "Step 5", "action": "extract", "selector": "#order", "variable": "order"}
    bad = [
        ({"steps": [{"action": "click", "selector": "#a"}]}, "starts by opening a URL"),
        ({"steps": [{"action": "navigate", "url": "http://169.254.169.254/"}]}, "public address"),
        ({"steps": [{"action": "navigate", "url": "https://a.io"}, {"action": "type", "selector": "#p", "text": "{nope}"}]}, "uses {nope}"),
        ({"steps": [{"action": "navigate", "url": "https://a.io"}, {"action": "press", "key": "Enter; rm"}]}, "key"),
        ({"steps": [{"action": "navigate", "url": "https://a.io"}, {"action": "run_js", "script": "x"}]}, "action: one of"),
        ({"steps": [{"action": "navigate", "url": "https://a.io"}, {"action": "wait", "ms": 60000}]}, "wait: 1-10000"),
        ({"steps": [{"action": "navigate", "url": "https://a.io"}, {"action": "click"}]}, "selector: 1-512"),
        ({"device": "fridge"}, "device"),
        ({"timeout_ms": 90000}, "1000-60000"),
    ]
    for change, reason in bad:
        with pytest.raises(synthetics.Refused, match=re.escape(reason)):
            synthetics.validate({**browser_settings(), "secret_names": ["password"], **change})


def test_browser_check_api_run_and_screenshots(aws, monkeypatch):
    boto3.client("s3").create_bucket(Bucket="obs-data-test")
    monkeypatch.setattr(synthetics, "DATA_BUCKET", "obs-data-test")
    monkeypatch.setattr(synthetics, "_s3", None)
    calls = fake_browser(monkeypatch)
    s, a = call("acme", "POST", "/v1/app/checks", {**browser_settings(), "secrets": {"password": PASSWORD}})
    assert s == 201 and a["type"] == "browser" and a["secret_names"] == ["password"]
    # Test: runs unsaved settings with the typed secret; the screenshot comes back inline, nothing stored.
    s, t = call("acme", "POST", "/v1/app/checks/test", {**browser_settings(), "secrets": {"password": PASSWORD}})
    assert s == 200 and t["result"]["steps"][0]["screenshot"] == JPEG
    assert calls[-1]["secrets"] == {"password": PASSWORD} and calls[-1]["budget_ms"] == synthetics.BROWSER_TEST_MS
    assert "pk" not in calls[-1]["check"] and "secrets" not in calls[-1]["check"]
    # Run now: recorded, screenshot stored under the tenant's prefix, shown through the API.
    s, r = call("acme", "POST", "/v1/app/checks/{id}/run", None, a["id"])
    assert s == 200 and not r["result"]["ok"]
    assert "pk" not in calls[-1]["check"] and "secrets" not in calls[-1]["check"] and calls[-1]["secrets"] == {"password": PASSWORD}
    run_id = r["result"]["run_id"]
    keys = [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="obs-data-test")["Contents"]]
    assert keys == [f"synthetics/tenant=acme/{a['id']}/{run_id}/1.jpg"]
    shot = lambda tenant, q: call_q(tenant, "/v1/app/checks/{id}/screenshot", a["id"], q)   # noqa: E731
    s, img = shot("acme", {"run": run_id, "step": "1"})
    assert s == 200 and img["image"] == JPEG
    assert shot("globex", {"run": run_id, "step": "1"})[0] == 404                            # not their check
    assert shot("acme", {"run": run_id, "step": "2"})[0] == 404
    assert shot("acme", {"run": "../../x", "step": "1"})[0] == 400
    # The recorded run: trace id = run id, browser details on the step, vitals as metrics, details in the log.
    docs = {st.rsplit("-", 1)[1]: b"".join(gzip.decompress(x) if x[:2] == b"\x1f\x8b" else x for x in recs) for st, recs in aws}
    assert run_id.encode() in docs["traces"] and b"step.lcp_ms" in docs["traces"] and b'"step.screenshot"' in docs["traces"]
    root = [sp for line in docs["traces"].splitlines() if line.strip()
            for rs in json.loads(line)["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"] if "parentSpanId" not in sp]
    assert {a["key"]: a["value"] for a in root[0]["attributes"]}["check.screenshots"] == {"stringValue": "1"}
    for m in (b"synthetics.browser.lcp", b"synthetics.browser.cls", b"synthetics.browser.ttfb"):
        assert m in docs["metrics"]
    assert b"TypeError: x is undefined" in docs["logs"] and b"10.0.0.9" in docs["logs"]
    assert PASSWORD.encode() not in b"".join(docs.values())


def call_q(tenant, resource, check_id, query):
    event = {"httpMethod": "GET", "resource": resource, "pathParameters": {"id": check_id}, "queryStringParameters": query,
             "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": "x@y.io"}}}}
    r = synthetics.api(event, None)
    return r["statusCode"], json.loads(r["body"])


def test_browser_function_failure_is_the_checks_failure(aws, monkeypatch):
    def invoke(**kw):
        return {"FunctionError": "Unhandled", "Payload": type("P", (), {"read": lambda self: json.dumps(
            {"errorMessage": f"Task timed out; secret {PASSWORD}"}).encode()})()}
    monkeypatch.setattr(synthetics, "_lambda", type("L", (), {"invoke": staticmethod(invoke)})())
    c = synthetics.validate({**browser_settings(), "secret_names": ["password"]})
    r = synthetics.run_any(c, {"password": PASSWORD})
    assert not r["ok"] and "the browser could not run" in r["failure"] and PASSWORD not in r["failure"]


def test_tick_sends_browser_checks_in_small_batches(aws, monkeypatch):
    monkeypatch.setattr(synthetics, "MAX_CHECKS", 50)
    for i in range(12):
        call("acme", "POST", "/v1/app/checks", {**browser_settings(name=f"b{i}", frequency=1), "secrets": {"password": "x"}})
    for i in range(3):
        call("acme", "POST", "/v1/app/checks", settings(name=f"h{i}", frequency=1))
    invoked = []
    monkeypatch.setattr(synthetics.boto3, "client", lambda name: type("L", (), {
        "invoke": lambda self, **kw: invoked.append(json.loads(kw["Payload"]))})())
    assert synthetics.tick({}, None) == {"due": 15}
    assert [(len(p["checks"]), {c.get("type", "http") for c in p["checks"]}) for p in invoked] == \
        [(3, {"http"}), (5, {"browser"}), (5, {"browser"}), (2, {"browser"})]


def test_each_recorded_run_is_passed_to_alerts(aws, monkeypatch):
    told = []
    monkeypatch.setattr(synthetics, "ALERTS_FUNCTION", "obs-alerts")
    monkeypatch.setattr(synthetics, "_lambda", type("L", (), {"invoke": staticmethod(lambda **kw: told.append(kw))})())
    result = {"ok": False, "failure": "Home: status 503", "failed_step": None, "total_ms": 5.0, "tls_days": None, "started": 1.0,
              "run_id": "c" * 32, "excluded": None, "steps": []}
    synthetics.record("acme", "abc123abc123", {"name": "Home", "frequency": 1, "steps": [{"url": "https://example.com/"}]}, result)
    assert told[0]["InvocationType"] == "Event" and told[0]["FunctionName"] == "obs-alerts"
    assert json.loads(told[0]["Payload"]) == {"action": "on_result", "tenant": "acme", "check_id": "abc123abc123", "check_name": "Home",
                                              "ok": False, "failure": "Home: status 503", "excluded": None, "run_id": "c" * 32}
    # If alerting can't be reached, the run is still recorded.
    monkeypatch.setattr(synthetics, "_lambda", type("L", (), {"invoke": staticmethod(lambda **kw: 1 / 0)})())
    synthetics.record("acme", "abc123abc123", {"name": "Home", "frequency": 1, "steps": [{"url": "https://example.com/"}]}, result)


def test_test_and_run_now_are_limited_per_tenant(aws, monkeypatch):
    monkeypatch.setattr(synthetics, "MANUAL_RUNS_PER_MINUTE", 2)
    monkeypatch.setattr(synthetics, "run_any", lambda check, plain, ms: {"ok": True, "steps": [], "duration_ms": 1})
    monkeypatch.setattr(synthetics, "_view_result", lambda r: r)
    monkeypatch.setattr(synthetics, "record", lambda *a: None)
    monkeypatch.setattr(synthetics, "store_screenshots", lambda t, c, r: r)
    s, a = call("acme", "POST", "/v1/app/checks", settings())
    assert call("acme", "POST", "/v1/app/checks/test", settings())[0] == 200
    assert call("acme", "POST", "/v1/app/checks/{id}/run", None, a["id"])[0] == 200
    s, out = call("acme", "POST", "/v1/app/checks/test", settings())
    assert s == 429 and "at most 2 test runs a minute" in out["error"]
    assert call("acme", "POST", "/v1/app/checks/{id}/run", None, a["id"])[0] == 429
    assert call("globex", "POST", "/v1/app/checks/test", settings())[0] == 200      # each tenant its own


def test_tick_skips_tenants_whose_free_trial_ended(aws, monkeypatch):
    call("acme", "POST", "/v1/app/checks", settings(name="mine", frequency=1))
    call("globex", "POST", "/v1/app/checks", settings(name="theirs", frequency=1))
    call("initech", "POST", "/v1/app/checks", settings(name="still trying", frequency=1))
    synthetics.table().put_item(Item={"pk": "tenant#globex", "tenant": "globex", "trial_ends_at": "2020-01-01T00:00:00Z"})
    synthetics.table().put_item(Item={"pk": "tenant#initech", "tenant": "initech", "trial_ends_at": "2099-01-01T00:00:00Z"})
    invoked = []
    monkeypatch.setattr(synthetics.boto3, "client", lambda name: type("L", (), {
        "invoke": lambda self, **kw: invoked.append(json.loads(kw["Payload"]))})())
    assert synthetics.tick({}, None) == {"due": 2}
    assert sorted(c["tenant"] for p in invoked for c in p["checks"]) == ["acme", "initech"]
