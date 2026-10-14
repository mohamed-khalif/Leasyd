"""Web monitoring: POST /v1/rum with a public browser key, from an allowed origin only."""

import gzip
import json
import time

import pytest

import ingest
import rum
from test_ingest import FakeFirehose  # noqa: F401  (also sets the AWS test environment)

KEY = "pub_" + "a" * 32


class Ddb:
    def __init__(self, items):
        self.items, self.gets = items, 0

    def get_item(self, TableName, Key, **kw):
        self.gets += 1
        return {"Item": self.items[Key["pk"]["S"]]} if Key["pk"]["S"] in self.items else {}


def key_item(status="active", scope="browser", origins=("https://shop.example.com",)):
    return {"tenant": {"S": "acme"}, "scope": {"S": scope}, "status": {"S": status},
            "origins": {"L": [{"S": o} for o in origins]}}


@pytest.fixture
def env(monkeypatch):
    fh = FakeFirehose()
    monkeypatch.setattr(ingest, "firehose", fh)
    monkeypatch.setattr(ingest, "TENANTS_TABLE", "obs-tenants")
    monkeypatch.setattr(ingest, "RECORD_COMPRESSION", "")
    ddb = Ddb({f"key#{rum._sha256(KEY)}": key_item()})
    monkeypatch.setattr(ingest, "ddb", ddb)
    monkeypatch.setattr(ingest.meter, "closed", lambda t: None)
    monkeypatch.setattr(ingest.meter, "over_cap", lambda t: None)
    monkeypatch.setattr(ingest.meter, "drop_rules", lambda t: [])
    added = []
    monkeypatch.setattr(ingest.meter, "add", lambda *a, **kw: added.append((a, kw)))
    rum._keys.clear()
    return fh, ddb, added


def post(body, origin="https://shop.example.com", key=KEY, method="POST", ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"):
    headers = {"User-Agent": ua, "Content-Type": "text/plain"}
    if origin:
        headers["Origin"] = origin
    out = ingest.handler({"path": "/ingest/v1/rum", "httpMethod": method, "headers": headers,
                          "queryStringParameters": {"k": key} if key else None, "body": json.dumps(body)}, None)
    return out


def stored(fh):
    docs = [json.loads(line) for _, data in fh.puts for line in data.decode().splitlines() if line]
    return [(d["resourceLogs"][0]["resource"], r) for d in docs for r in d["resourceLogs"][0]["scopeLogs"][0]["logRecords"]]


def attrs(r):
    return {a["key"]: list(a["value"].values())[0] for a in r["attributes"]}


def events():
    now = int(time.time() * 1000)
    return [{"type": "page_view", "t": now, "page": "/checkout", "pv": "ab12cd34ef56ab78", "title": "Checkout", "load_ms": 1834.5, "ttfb_ms": 210},
            {"type": "web_vital", "t": now, "page": "/checkout", "pv": "ab12cd34ef56ab78", "name": "LCP", "value": 4630, "rating": "poor"},
            {"type": "js_error", "t": now, "page": "/checkout", "message": "TypeError: x is undefined", "source": "/app.js", "line": 12, "column": 7, "stack": "at f (app.js:12:7)"},
            {"type": "api_call", "t": now, "page": "/checkout", "method": "post", "url": "/api/pay", "status": 503, "duration_ms": 1200, "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"},
            {"type": "bogus", "t": now},
            {"type": "web_vital", "t": now, "name": "XYZ", "value": 1}]


def test_events_become_the_tenants_logs(env):
    fh, _, added = env
    out = post({"app": "shop-web", "env": "prod", "sid": "0123456789abcdef0123456789abcdef", "events": events()})
    assert out["statusCode"] == 200 and out["headers"]["Access-Control-Allow-Origin"] == "https://shop.example.com"
    assert {name for name, _ in fh.puts} == {"obs-t-acme-logs"}
    recs = stored(fh)
    assert len(recs) == 4                                       # unknown types and vitals dropped
    res = {a["key"]: list(a["value"].values())[0] for a in recs[0][0]["attributes"]}
    assert res["service.name"] == "shop-web" and res["deployment.environment"] == "prod"
    assert res["browser.name"] == "Safari" and res["os.name"] == "iOS" and res["device.type"] == "mobile"
    pv, lcp, err, call = (r for _, r in recs)
    assert pv["body"]["stringValue"] == "page_view /checkout" and attrs(pv)["rum.load_ms"] == 1834.5
    assert attrs(pv)["rum.session_id"] == "0123456789abcdef0123456789abcdef"
    assert lcp["severityText"] == "WARN" and attrs(lcp)["rum.vital.name"] == "LCP" and attrs(lcp)["rum.vital.value"] == 4630
    assert err["severityText"] == "ERROR" and attrs(err)["error.message"] == "TypeError: x is undefined"
    assert call["severityText"] == "ERROR" and call["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert call["body"]["stringValue"] == "POST /api/pay 503 1200 ms"
    assert added and added[-1][0][0] == "acme"                  # metered like any log


def test_only_a_live_browser_key_from_an_allowed_origin(env):
    fh, ddb, _ = env
    body = {"app": "shop-web", "events": events()[:1]}
    assert post(body, origin="https://evil.example.org")["statusCode"] == 403
    assert post(body, origin=None)["statusCode"] == 403          # no Origin and no Referer
    assert post(body, key="obs_" + "a" * 40)["statusCode"] == 401   # an API key isn't a browser key
    assert post(body, key=None)["statusCode"] == 401
    ddb.items[f"key#{rum._sha256('pub_' + 'b' * 32)}"] = key_item(status="revoked")
    assert post(body, key="pub_" + "b" * 32)["statusCode"] == 401
    ddb.items[f"key#{rum._sha256('pub_' + 'c' * 32)}"] = key_item(scope="ingest")
    assert post(body, key="pub_" + "c" * 32)["statusCode"] == 401
    assert fh.puts == []
    rum._keys.clear()
    gets = ddb.gets
    post(body); post(body)
    assert ddb.gets == gets + 1                                   # the key is cached for a minute
    pre = post(body, method="OPTIONS")
    assert pre["statusCode"] == 204 and pre["headers"]["Access-Control-Allow-Methods"] == "POST"


def test_limits(env, monkeypatch):
    fh, _, _ = env
    big = {"app": "x", "events": [{"type": "page_view", "page": "/" + "a" * 70000}]}
    assert post(big)["statusCode"] == 413
    many = {"app": "x" * 200, "events": [dict(events()[0]) for _ in range(250)]}
    assert post(many)["statusCode"] == 200
    recs = stored(fh)
    assert len(recs) == rum.MAX_EVENTS
    assert {a["key"]: a["value"] for a in recs[0][0]["attributes"]}["service.name"]["stringValue"] == "x" * 64
    monkeypatch.setattr(ingest.meter, "over_cap", lambda t: 10**9)
    assert post({"app": "x", "events": events()[:1]})["statusCode"] == 429


def test_user_agents():
    assert rum.browser("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0") == \
        {"browser.name": "Edge", "browser.version": "129", "os.name": "Windows", "device.type": "desktop"}
    assert rum.browser("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36")["device.type"] == "mobile"
    assert rum.browser("")["device.type"] == "unknown"
