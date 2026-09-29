import gzip
import http.server
import ipaddress
import json
import os
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

REAL_PUBLIC = synthetics.public


# ------------------------------------------------------------------ a local site to check

class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(1.5)
        if self.path == "/to-inside":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.8/admin")
            self.end_headers()
            return
        if self.path == "/to-ok":
            self.send_response(301)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        status = 500 if self.path == "/broken" else 200
        body = b"Welcome to Acme" if status == 200 else b"oops"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def site(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # The local test site is on loopback: allow exactly that, everything else stays checked for real.
    monkeypatch.setattr(synthetics, "public", lambda ip: ip.is_loopback or REAL_PUBLIC(ip))
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def check(url, **kw):
    return synthetics.validate({"name": "t", "url": url, "timeout_ms": 1000 if "slow" in url else 5000, **kw})


# ------------------------------------------------------------------ never reach inside

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.1.2.3/", "http://192.168.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://[::1]/", "http://[fd00::1]/", "http://[::ffff:10.0.0.1]/", "http://0.0.0.0/",
    "http://localhost:9001/2018-06-01/runtime/invocation/next", "http://metadata.internal/", "ftp://example.com/",
    "http://user:pass@example.com/", "file:///etc/passwd", "http:///nohost",
])
def test_non_public_or_odd_urls_are_refused(url):
    with pytest.raises(synthetics.Refused):
        check(url)


def test_a_name_that_resolves_inside_is_refused(monkeypatch):
    def fake(host, port, **kw):
        ips = {"good.example": ["93.184.216.34"], "sneaky.example": ["93.184.216.34", "10.0.0.5"]}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert synthetics.resolve("good.example", 443) == ["93.184.216.34"]
    with pytest.raises(synthetics.Refused, match="non-public"):
        synthetics.resolve("sneaky.example", 443)
    r = synthetics.probe(check("https://sneaky.example/"))
    assert not r["ok"] and "non-public" in r["failure"]


def test_public_means_globally_routable():
    assert REAL_PUBLIC(ipaddress.ip_address("93.184.216.34"))
    assert REAL_PUBLIC(ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946"))
    for ip in ("10.0.0.1", "172.16.0.1", "169.254.169.254", "127.0.0.1", "100.64.0.1", "224.0.0.1", "::1", "fe80::1"):
        assert not REAL_PUBLIC(ipaddress.ip_address(ip)), ip


def test_other_settings_are_validated():
    for bad in ({"frequency": 2}, {"method": "DELETE"}, {"timeout_ms": 60_000}, {"expect": {"status": "2x"}},
                {"headers": {"Host": "evil"}}, {"headers": {"X-A": "a\r\nX-B: b"}}, {"body": "x"}, {"name": ""}):
        with pytest.raises(synthetics.Refused):
            synthetics.validate({"name": "t", "url": "https://example.com/", **bad})
    c = synthetics.validate({"name": " Home ", "url": "https://example.com/"})
    assert c["name"] == "Home" and c["frequency"] == 5 and c["expect"] == {"status": "2xx"} and c["enabled"]


# ------------------------------------------------------------------ probing a real server

def test_probe_passes_and_measures(site):
    r = synthetics.probe(check(site + "/ok", expect={"status": "200", "contains": "Welcome"}))
    assert r["ok"] and r["status"] == 200 and r["failure"] is None
    t = r["timings"]
    assert 0 <= t["dns_ms"] <= t["connect_ms"] <= t["ttfb_ms"] <= t["total_ms"] < 2000


@pytest.mark.parametrize("path,expect,why", [
    ("/broken", {"status": "2xx"}, "status 500, expected 2xx"),
    ("/ok", {"status": "2xx", "contains": "Goodbye"}, "does not contain 'Goodbye'"),
    ("/ok", {"status": "3xx,404"}, "status 200, expected 3xx,404"),
])
def test_probe_fails_with_a_reason(site, path, expect, why):
    r = synthetics.probe(check(site + path, expect=expect))
    assert not r["ok"] and why in r["failure"]


def test_timeouts_and_slow_answers(site):
    r = synthetics.probe(check(site + "/slow"))                       # timeout 1 s, server takes 1.5 s
    assert not r["ok"] and r["failure"] == "timed out"
    r = synthetics.probe(check(site + "/ok", expect={"status": "2xx", "max_ms": 1}))
    assert r["ok"] or "expected under 1 ms" in r["failure"]


def test_redirects_are_followed_but_never_inside(site):
    assert synthetics.probe(check(site + "/to-ok"))["ok"]
    assert synthetics.probe(check(site + "/to-ok", follow_redirects=False, expect={"status": "301"}))["ok"]
    r = synthetics.probe(check(site + "/to-inside"))
    assert not r["ok"] and "public address" in r["failure"]


# ------------------------------------------------------------------ API, per tenant

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
        monkeypatch.setattr(synthetics, "_table", None)
        sent = []
        monkeypatch.setattr(synthetics.ingest, "put_records", lambda stream, records: sent.append((stream, records)))
        yield sent


def call(tenant, method, resource, body=None, check_id=None):
    event = {"httpMethod": method, "resource": resource, "pathParameters": {"id": check_id} if check_id else None,
             "body": json.dumps(body) if body is not None else None,
             "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": f"ana@{tenant}.io"}}}}
    r = synthetics.api(event, None)
    return r["statusCode"], json.loads(r["body"])


def test_crud_and_each_tenant_sees_only_its_own_checks(aws):
    s, a = call("acme", "POST", "/v1/app/checks", {"name": "Home", "url": "https://example.com/", "tenant": "globex"})
    assert s == 201 and a["name"] == "Home" and "tenant" not in a
    call("globex", "POST", "/v1/app/checks", {"name": "Theirs", "url": "https://example.org/"})
    assert [c["name"] for c in call("acme", "GET", "/v1/app/checks")[1]["checks"]] == ["Home"]
    assert [c["name"] for c in call("globex", "GET", "/v1/app/checks")[1]["checks"]] == ["Theirs"]
    for method, resource in (("GET", "/v1/app/checks/{id}"), ("PUT", "/v1/app/checks/{id}"),
                             ("DELETE", "/v1/app/checks/{id}"), ("POST", "/v1/app/checks/{id}/run")):
        assert call("globex", method, resource, {}, a["id"])[0] == 404        # someone else's check
    s, u = call("acme", "PUT", "/v1/app/checks/{id}", {"frequency": 1, "enabled": False}, a["id"])
    assert s == 200 and u["frequency"] == 1 and not u["enabled"] and u["url"] == "https://example.com/"
    assert call("acme", "PUT", "/v1/app/checks/{id}", {"url": "http://10.0.0.1/"}, a["id"])[0] == 400
    assert call("acme", "DELETE", "/v1/app/checks/{id}", None, a["id"])[0] == 200
    assert call("acme", "GET", "/v1/app/checks")[1]["checks"] == []
    assert call("acme", "GET", "/v1/app/checks/{id}", None, "../../x")[0] == 404
    ev = {"httpMethod": "GET", "resource": "/v1/app/checks", "requestContext": {"authorizer": {"claims": {}}}}
    assert synthetics.api(ev, None)["statusCode"] == 401


def test_limit_and_bad_input(aws, monkeypatch):
    monkeypatch.setattr(synthetics, "MAX_CHECKS", 2)
    for i in range(2):
        assert call("acme", "POST", "/v1/app/checks", {"name": f"c{i}", "url": "https://example.com/"})[0] == 201
    s, e = call("acme", "POST", "/v1/app/checks", {"name": "c3", "url": "https://example.com/"})
    assert s == 400 and "at most 2" in e["error"]
    assert call("acme", "POST", "/v1/app/checks", {"name": "x", "url": "http://169.254.169.254/"})[0] == 400


def test_run_now_records_telemetry_for_that_tenant(aws, site):
    _, a = call("acme", "POST", "/v1/app/checks", {"name": "Home", "url": site + "/ok"})
    s, r = call("acme", "POST", "/v1/app/checks/{id}/run", None, a["id"])
    assert s == 200 and r["result"]["ok"]
    assert sorted(stream for stream, _ in aws) == ["obs-t-acme-metrics", "obs-t-acme-traces"]
    s, r = call("acme", "POST", "/v1/app/checks/test", {"name": "Try", "url": site + "/broken"})
    assert s == 200 and not r["result"]["ok"] and len(aws) == 2          # a test run is not recorded


# ------------------------------------------------------------------ schedule and results

def test_due_spreads_checks_over_their_period():
    ids = [f"{i:012x}" for i in range(0, 3000, 7)]
    for f in (1, 5, 15):
        runs = [sum(synthetics.due(c, f, m) for m in range(60)) for c in ids]
        assert set(runs) == {60 // f}                                   # each runs exactly every f minutes
    per_minute = [sum(synthetics.due(c, 15, m) for c in ids) for m in range(15)]
    assert max(per_minute) - min(per_minute) <= 2                       # and they don't all run at once


def test_tick_hands_due_checks_to_the_runner(aws, monkeypatch):
    monkeypatch.setattr(synthetics, "MAX_CHECKS", 50)
    for i in range(30):
        call("acme", "POST", "/v1/app/checks", {"name": f"c{i}", "url": "https://example.com/", "frequency": 1})
    call("acme", "POST", "/v1/app/checks", {"name": "off", "url": "https://example.com/", "frequency": 1, "enabled": False})
    invoked = []
    monkeypatch.setattr(synthetics.boto3, "client", lambda name: type("L", (), {
        "invoke": lambda self, **kw: invoked.append(json.loads(kw["Payload"]))})())
    assert synthetics.tick({}, None) == {"due": 30}
    assert [len(p["checks"]) for p in invoked] == [25, 5]
    assert all(c["tenant"] == "acme" and c["enabled"] for p in invoked for c in p["checks"])


def test_results_are_valid_telemetry_the_pipeline_accepts(tmp_path):
    import compact
    import duckdb
    import ingest
    c = check("https://example.com/", expect={"status": "2xx"})
    ok = {"ok": True, "status": 200, "failure": None, "url": c["url"], "started": 1_790_000_000.0, "tls_days": 61.5,
          "timings": {"dns_ms": 3.0, "connect_ms": 20.0, "tls_ms": 45.0, "ttfb_ms": 120.0, "total_ms": 130.5}}
    bad = {**ok, "ok": False, "status": 503, "failure": "status 503, expected 2xx", "tls_days": None}
    for name, result in (("ok", ok), ("bad", bad)):
        for signal, doc in synthetics.telemetry("abc123abc123", c, result).items():
            ingest.parse(signal, json.dumps(doc).encode(), "application/json")
            f = tmp_path / f"{name}-{signal}.json.gz"
            f.write_bytes(gzip.compress(b"".join(ingest.to_records(signal, doc))))
            assert compact.compact(signal, [str(f)], str(tmp_path / name / signal), "b1", "2026-09-21", "12")
    span = duckdb.sql(f"SELECT service, name, status_code, attributes['check.result'], attributes['http.response.status_code'] "
                      f"FROM '{tmp_path}/bad/traces/**/*.parquet'").fetchall()
    assert span == [("synthetics", "t", 2, "fail", "503")]                   # OTLP status code 2: error
    metrics = duckdb.sql(f"SELECT metric_name, value FROM '{tmp_path}/ok/metrics/**/*.parquet' ORDER BY 1").fetchall()
    assert metrics == [("synthetics.check.duration", 130.5), ("synthetics.check.success", 1.0),
                       ("synthetics.check.tls_days_remaining", 61.5)]
    logs = duckdb.sql(f"SELECT severity_text, body FROM '{tmp_path}/bad/logs/**/*.parquet'").fetchall()
    assert logs == [("ERROR", "Check 't' failed: status 503, expected 2xx")]
    assert "logs" not in synthetics.telemetry("abc123abc123", c, ok)          # passing runs log nothing
