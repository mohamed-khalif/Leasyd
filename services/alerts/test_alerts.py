import base64
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import sys
import threading

import boto3
import pytest
from moto import mock_aws

HERE = os.path.dirname(__file__)
sys.path[:0] = [HERE, os.path.join(HERE, "..", "synthetics"), os.path.join(HERE, "..", "ingest")]
os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing")
os.environ.pop("AWS_SESSION_TOKEN", None)

import alerts  # noqa: E402
import safety  # noqa: E402

REAL_PUBLIC = safety.public


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "tenant", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                         GlobalSecondaryIndexes=[{"IndexName": "by-tenant", "Projection": {"ProjectionType": "ALL"},
                                                  "KeySchema": [{"AttributeName": "tenant", "KeyType": "HASH"}, {"AttributeName": "pk", "KeyType": "RANGE"}]}])
        monkeypatch.setattr(alerts, "KMS_KEY", boto3.client("kms").create_key()["KeyMetadata"]["KeyId"])
        monkeypatch.setattr(alerts, "_clients", {})
        logs = []
        monkeypatch.setattr(alerts.ingest, "put_records", lambda stream, records: logs.append((stream, records)))
        t = boto3.resource("dynamodb").Table("obs-tenants")
        for tenant in ("acme", "globex"):
            t.put_item(Item={"pk": f"check#{tenant}#aaaabbbbcccc", "tenant": tenant, "name": "Checkout API"})
            t.put_item(Item={"pk": f"slo#{tenant}#sssstttttttt", "tenant": tenant, "name": "Checkout up", "type": "availability",
                             "checks": ["aaaabbbbcccc"], "target": 99, "window_days": 7})
        yield logs


def call(tenant, method, path, body=None):
    ev = {"httpMethod": method, "pathParameters": {"proxy": path}, "isBase64Encoded": body is not None,
          "body": base64.b64encode(json.dumps(body).encode()).decode() if body is not None else None,
          "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": f"ana@{tenant}.io"}}}}
    r = alerts.api(ev)
    return r["statusCode"], json.loads(r["body"])


class Hook(http.server.BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Hook.got.append((self.path, dict(self.headers), body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def hook(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(safety, "public", lambda ip: ip == ipaddress.ip_address("127.0.0.1") or REAL_PUBLIC(ip))
    Hook.got = []
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook"
    srv.shutdown()


def webhook_channel(tenant, url):
    """A webhook channel pointing at the local test server (the API only accepts https URLs)."""
    s, ch = call(tenant, "POST", "channels", {"type": "webhook", "name": "Ops webhook", "url": "https://hooks.example.com/x"})
    assert s == 201 and len(ch["signing_secret"]) == 48
    t = boto3.resource("dynamodb").Table("obs-tenants")
    item = t.get_item(Key={"pk": f"channel#{tenant}#{ch['id']}"})["Item"]
    item["secrets"] = alerts._encrypt(tenant, ch["id"], {"url": url, "secret": ch["signing_secret"]})
    t.put_item(Item=item)
    return ch


def test_channels(aws):
    s, email = call("acme", "POST", "channels", {"type": "email", "name": "On call", "email": "Oncall@Acme.io"})
    assert s == 201 and email["email"] == "oncall@acme.io" and "topic_arn" not in email
    topics = [t["TopicArn"] for t in boto3.client("sns").list_topics()["Topics"]]
    assert any(t.endswith(f":obs-alert-acme-{email['id']}") for t in topics)
    s, slack = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/abcdSECRET"})
    assert s == 201 and slack["url_hint"] == "https://hooks.slack.com/…CRET"
    assert "abcdSECRET" not in json.dumps(call("acme", "GET", "channels")[1], ensure_ascii=False)   # the URL never comes back
    stored = boto3.resource("dynamodb").Table("obs-tenants").get_item(Key={"pk": f"channel#acme#{slack['id']}"})["Item"]
    assert "abcdSECRET" not in json.dumps(stored, default=str)                         # encrypted at rest
    for bad, why in [({"type": "slack", "name": "x", "url": "https://evil.example.com/services/x"}, "Slack incoming-webhook"),
                     ({"type": "webhook", "name": "x", "url": "http://example.com/x"}, "https://"),
                     ({"type": "webhook", "name": "x", "url": "https://169.254.169.254/latest"}, "public address"),
                     ({"type": "email", "name": "x", "email": "not-an-email"}, "not an email"),
                     ({"type": "pager", "name": "x"}, "email, slack or webhook")]:
        s, e = call("acme", "POST", "channels", bad)
        assert s == 400 and why in e["error"], (bad, e)
    assert call("globex", "GET", "channels")[1]["items"] == []
    assert call("globex", "DELETE", f"channels/{email['id']}")[0] == 404
    assert call("acme", "DELETE", f"channels/{email['id']}")[0] == 200
    assert not any(t["TopicArn"].endswith(email["id"]) for t in boto3.client("sns").list_topics()["Topics"])


def test_rules_validation_and_isolation(aws, hook):
    ch = webhook_channel("acme", hook)
    good = {"name": "Checkout down", "type": "check_failing", "checks": ["aaaabbbbcccc"], "failures": 3, "channels": [ch["id"]]}
    s, r = call("acme", "POST", "rules", good)
    assert s == 201 and r["failures"] == 3
    for change, why in [({"channels": []}, "channels"), ({"checks": ["nope00000000"]}, "checks"), ({"failures": 20}, "1-10"),
                        ({"type": "slo_burn", "slo": "sssstttttttt"}, "burn_rate, budget_below"), ({"type": "x"}, "check_failing, slo_burn or query")]:
        s, e = call("acme", "POST", "rules", {**good, **change})
        assert s == 400 and why in e["error"], (change, e)
    assert call("globex", "POST", "rules", good)[0] == 400                 # not their channel
    assert call("globex", "GET", "rules")[1]["items"] == []
    assert call("globex", "PUT", f"rules/{r['id']}", {"failures": 1})[0] == 404
    assert "used by: Checkout down" in call("acme", "DELETE", f"channels/{ch['id']}")[1]["error"]


def test_check_failing_fires_once_and_resolves(aws, hook):
    ch = webhook_channel("acme", hook)
    s, rule = call("acme", "POST", "rules", {"name": "Checkout down", "type": "check_failing", "checks": ["*"], "failures": 2, "channels": [ch["id"]]})
    run = lambda ok, **kw: alerts.on_result({"tenant": "acme", "check_id": "aaaabbbbcccc", "check_name": "Checkout API", "ok": ok,  # noqa: E731
                                             "failure": None if ok else "Create cart: status 503", **kw})
    assert run(False)["fired"] == []                                      # 1 of 2
    assert run(False, excluded="maintenance window: Deploy")["skipped"]  # doesn't count either way
    assert run(False)["fired"] == [(rule["id"], "firing")]                # 2 in a row
    assert run(False)["fired"] == []                                      # still failing: no repeat
    assert call("acme", "GET", "rules")[1]["items"][0]["firing"] == ["aaaabbbbcccc"]
    assert run(True)["fired"] == [(rule["id"], "resolved")]
    assert run(True)["fired"] == []
    # Other tenants' runs never touch this rule.
    assert alerts.on_result({"tenant": "globex", "check_id": "aaaabbbbcccc", "check_name": "x", "ok": False})["fired"] == []
    # Two webhooks, signed with the channel's secret; the payload says what happened.
    assert len(Hook.got) == 2
    for path, headers, body in Hook.got:
        expected = hmac.new(ch["signing_secret"].encode(), body, hashlib.sha256).hexdigest()
        assert headers["X-Leasyd-Signature"] == f"sha256={expected}"
    first, second = (json.loads(b) for _, _, b in Hook.got)
    assert first["state"] == "firing" and "Failed 2 runs in a row" in first["detail"] and "503" in first["detail"]
    assert second["state"] == "resolved" and first["url"].endswith("/#/synthetics/aaaabbbbcccc")
    # And the history, as the tenant's own logs.
    assert [s for s, _ in aws] == ["obs-t-acme-logs", "obs-t-acme-logs"]


def test_webhook_to_an_inside_address_is_refused_when_sending(aws, hook, monkeypatch):
    ch = webhook_channel("acme", "http://10.0.0.5/hook")
    item = alerts._get(f"channel#acme#{ch['id']}", "acme")
    err = alerts.notify("acme", item, alerts.message("acme", {"pk": "alert#acme#000000000000", "name": "x", "type": "t"}, "firing", "s", "d", "l"))
    assert err and "public address" in err
    assert call("acme", "POST", f"channels/{ch['id']}/test")[1]["sent"] is False


def test_slo_burn(aws, hook, monkeypatch):
    ch = webhook_channel("acme", hook)
    s, rule = call("acme", "POST", "rules", {"name": "Budget", "type": "slo_burn", "slo": "sssstttttttt", "burn_rate": 10,
                                             "budget_below": 25, "channels": [ch["id"]]})
    assert s == 201
    numbers = {"week": (995, 1000), "hour": (60, 60)}                    # 0.5% bad of a 1% budget: 50% left, no burn
    def fake_query(q):
        good, total = numbers["hour" if q["start"] > "2000" and alerts.datetime.fromisoformat(q["start"].replace("Z", "+00:00")) >
                               alerts.datetime.now(alerts.timezone.utc) - alerts.timedelta(hours=2) else "week"]
        assert {"field": "attributes.check.excluded", "op": "not_exists"} in q["where"] and q["tenant"] == "acme"
        return {"columns": ["sum(value)", "count"], "rows": [[good, total]]}
    monkeypatch.setattr(alerts, "_query", fake_query)
    assert alerts.evaluate()["evaluated"] == [rule["id"]] and Hook.got == []
    numbers["hour"] = (50, 60)                                            # 17% bad in the last hour: 16.7x the budget rate
    alerts.evaluate()
    assert len(Hook.got) == 1 and "16.7× too fast" in json.loads(Hook.got[0][2])["detail"]
    alerts.evaluate()
    assert len(Hook.got) == 1                                             # still firing: no repeat
    numbers.update(week=(998, 1000), hour=(60, 60))
    alerts.evaluate()
    assert len(Hook.got) == 2 and json.loads(Hook.got[1][2])["state"] == "resolved"
    assert alerts.evaluate_numbers(99.9, 997, 1000)["budget_left"] == pytest.approx(-2.0)


# ------------------------------------------------------------------ check rules (PromQL)

class FakeProm:
    """The query engine's answer to {"tenant", "promql", "time"}: whatever the test sets."""
    def __init__(self):
        self.series, self.calls, self.error = [], [], None

    def __call__(self, q):
        self.calls.append(q)
        if self.error:
            return {"error": self.error}
        assert "promql" in q and isinstance(q["time"], int)
        return {"status": "success", "data": {"resultType": "vector",
                "result": [{"metric": m, "value": [q["time"], str(v)]} for m, v in self.series]}}


def query_rule(tenant, channel, **kw):
    body = {"name": "Error rate", "type": "query", "channels": [channel], "promql": "sum by (service_name) (rate(leasyd.spans[5m]))",
            "op": ">", "critical": 5, "degraded": 2, **kw}
    s, r = call(tenant, "POST", "rules", body)
    assert s == 201, r
    return r


def test_check_rule_validation(aws, monkeypatch):
    prom = FakeProm()
    monkeypatch.setattr(alerts, "_invoke", prom)
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    for bad, why in [({"op": "~"}, "op: one of"), ({"critical": "high"}, "critical: a number"), ({"critical": None}, "critical: a number"),
                     ({"degraded": 9}, "degraded must come before critical"), ({"for_minutes": 90}, "for_minutes"),
                     ({"every_minutes": 2}, "every_minutes"), ({"promql": ""}, "promql")]:
        body = {"name": "x", "type": "query", "channels": [ch], "promql": "up", "op": ">", "critical": 5, **bad}
        s, e = call("acme", "POST", "rules", body)
        assert s == 400 and why in e["error"], (bad, e)
    prom.error = "expected , or ) but found the end at position 11"
    s, e = call("acme", "POST", "rules", {"name": "x", "type": "query", "channels": [ch], "promql": "rate(x[5m]", "op": ">", "critical": 1})
    assert s == 400 and "expected , or )" in e["error"]                       # a broken query is refused when saved
    prom.error = None
    assert query_rule("acme", ch)["promql"].startswith("sum by")
    assert call("globex", "GET", "rules")[1]["items"] == []                   # rules are per tenant
    prom.series = [({"service_name": "checkout"}, 7.0), ({"service_name": "cart"}, 3.0), ({"service_name": "ad"}, 0.5)]
    s, p = call("acme", "POST", "rules/preview", {"promql": "x", "op": ">", "critical": 5, "degraded": 2})
    assert s == 200 and [(x["name"], x["level"]) for x in p["series"]] == [("service_name=checkout", "critical"), ("service_name=cart", "degraded"), ("service_name=ad", "ok")]
    assert prom.calls[-1]["tenant"] == "acme"                                 # always the caller's tenant


def test_check_rule_fires_escalates_and_resolves_per_series(aws, monkeypatch):
    from datetime import datetime, timedelta, timezone
    prom, sent = FakeProm(), []
    monkeypatch.setattr(alerts, "_invoke", prom)
    monkeypatch.setattr(alerts, "notify", lambda tenant, channel, msg: sent.append(msg) and None)
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    rule = query_rule("acme", ch)
    item = alerts._plain(alerts._get(f"alert#acme#{rule['id']}", "acme"))
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    ev = lambda m: alerts.evaluate_query_rule(item, t0 + timedelta(minutes=m))  # noqa: E731
    prom.series = [({"service_name": "checkout"}, 7.0), ({"service_name": "cart"}, 3.0), ({"service_name": "ad"}, 0.5)]
    assert ev(0) == {"critical": 1, "degraded": 1, "ok": 0}
    titles = [m["title"] for m in sent]
    assert any("FIRING" in t and "service_name=checkout (critical)" in t for t in titles)
    assert any("FIRING" in t and "service_name=cart (degraded)" in t for t in titles)
    assert "rate(leasyd.spans" in sent[0]["text"] and "/#/query?promql=" in sent[0]["text"]
    s, listing = call("acme", "GET", "rules")
    assert sorted(listing["items"][0]["firing"]) == ["service_name=cart (degraded)", "service_name=checkout (critical)"]
    sent.clear()
    assert ev(1) == {"critical": 0, "degraded": 0, "ok": 0} and not sent      # nothing changed: no message
    prom.series = [({"service_name": "checkout"}, 6.0), ({"service_name": "cart"}, 9.0)]
    assert ev(2) == {"critical": 1, "degraded": 0, "ok": 0}                    # cart escalates
    assert "(was degraded)" in sent[0]["text"]
    sent.clear()
    prom.series = [({"service_name": "checkout"}, 1.0)]
    assert ev(3) == {"critical": 0, "degraded": 0, "ok": 2}                    # checkout ok, cart gone: both resolved, one message
    assert len(sent) == 1 and "RESOLVED" in sent[0]["title"] and "2 series" in sent[0]["title"]
    assert call("acme", "GET", "rules")[1]["items"][0]["firing"] == []
    assert not alerts._items("acme", f"astate#acme#{rule['id']}#")            # no state kept for healthy series
    logs = [json.loads(__import__("gzip").decompress(r)) if r[:2] == b"\x1f\x8b" else json.loads(r) for _, rs in aws for r in rs]
    assert len(logs) == 4                                                      # history: critical, degraded, escalated, resolved


def test_check_rule_waits_for_minutes(aws, monkeypatch):
    from datetime import datetime, timedelta, timezone
    prom, sent = FakeProm(), []
    monkeypatch.setattr(alerts, "_invoke", prom)
    monkeypatch.setattr(alerts, "notify", lambda tenant, channel, msg: sent.append(msg) and None)
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    item = alerts._plain(alerts._get(f"alert#acme#{query_rule('acme', ch, for_minutes=3, degraded=None)['id']}", "acme"))
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    prom.series = [({}, 9.0)]
    for m in (0, 1, 2):
        alerts.evaluate_query_rule(item, t0 + timedelta(minutes=m))
    assert not sent                                                            # held 2 minutes: not yet
    prom.series = [({}, 1.0)]
    alerts.evaluate_query_rule(item, t0 + timedelta(minutes=3))                # a blip back to normal resets the clock
    prom.series = [({}, 9.0)]
    for m in (4, 5, 6):
        alerts.evaluate_query_rule(item, t0 + timedelta(minutes=m))
    assert not sent
    alerts.evaluate_query_rule(item, t0 + timedelta(minutes=7))                # held 3 minutes
    assert len(sent) == 1 and "the query (critical)" in sent[0]["title"] and "for 3 min" in sent[0]["text"]


def test_schedule_runs_check_rules_every_minute_and_slos_every_five(aws, monkeypatch):
    prom = FakeProm()
    monkeypatch.setattr(alerts, "_invoke", prom)
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    every1, every5 = query_rule("acme", ch)["id"], query_rule("acme", ch, every_minutes=5)["id"]
    s, off = call("acme", "POST", "rules", {"name": "off", "type": "query", "channels": [ch], "promql": "x", "op": ">", "critical": 1, "enabled": False})
    seen = {}
    monkeypatch.setattr(alerts, "slo_status", lambda *a: seen.setdefault("slo", True) and {"attainment": None, "budget_left": None, "burn1h": None})
    out = alerts.evaluate({"time": "2026-10-01T12:01:00Z"})
    assert out["check_rules"] == [every1] and out["evaluated"] == []           # minute 1: 1-minute rules only
    out = alerts.evaluate({"time": "2026-10-01T12:05:00Z"})
    assert sorted(out["check_rules"]) == sorted([every1, every5])             # minute 5: both, and SLO rules too


# ------------------------------------------------------------------ dashboards

def dash_call(tenant, method, dash_id=None, body=None):
    ev = {"httpMethod": method, "resource": "/v1/app/dashboards" + ("/{proxy+}" if dash_id else ""),
          "pathParameters": {"proxy": dash_id} if dash_id else None, "isBase64Encoded": body is not None,
          "body": base64.b64encode(json.dumps(body).encode()).decode() if body is not None else None,
          "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": f"ana@{tenant}.io"}}}}
    r = alerts.api(ev)
    return r["statusCode"], json.loads(r["body"])


DASH = {"name": "Checkout service", "description": "RED for checkout", "variables": [{"name": "service_name", "label": "Service"}],
        "panels": [{"type": "text", "title": "About", "text": "Requests, errors, duration.", "w": 3, "h": 2},
                   {"type": "timeseries", "title": "Requests/s", "w": 9, "h": 2, "unit": "/s",
                    "queries": [{"promql": 'sum by (service_name) (rate(leasyd.spans{service_name=~"$service_name"}[5m]))', "legend": "{{service_name}}"}]}]}


def test_dashboards_crud_versions_and_isolation(aws):
    s, d = dash_call("acme", "POST", body=DASH)
    assert s == 201 and d["version"] == 1 and len(d["panels"]) == 2 and d["panels"][1]["id"]
    s, listing = dash_call("acme", "GET")
    assert s == 200 and [(x["name"], x["panels"]) for x in listing["items"]] == [("Checkout service", 2)]
    assert dash_call("globex", "GET")[1]["items"] == [] and dash_call("globex", "GET", d["id"])[0] == 404   # per tenant
    assert dash_call("globex", "PUT", d["id"], {**DASH, "version": 1})[0] == 404
    assert dash_call("globex", "DELETE", d["id"])[0] == 404
    # Two people edit version 1: the first save wins, the second is told instead of overwriting.
    s, d2 = dash_call("acme", "PUT", d["id"], {"name": "Checkout (v2)", "version": 1})
    assert s == 200 and d2["version"] == 2 and d2["name"] == "Checkout (v2)" and len(d2["panels"]) == 2
    s, e = dash_call("acme", "PUT", d["id"], {"name": "Someone else's edit", "version": 1})
    assert s == 409 and "ana@acme.io saved this dashboard" in e["error"] and e["version"] == 2
    assert dash_call("acme", "GET", d["id"])[1]["name"] == "Checkout (v2)"
    assert dash_call("acme", "DELETE", d["id"])[0] == 200 and dash_call("acme", "GET", d["id"])[0] == 404


@pytest.mark.parametrize("change, why", [
    ({"name": ""}, "name"), ({"panels": [{"type": "pie", "title": "x"}]}, "type is one of"),
    ({"panels": [{"type": "timeseries", "title": "x", "queries": []}]}, "1-5 queries"),
    ({"panels": [{"type": "timeseries", "title": "x", "queries": [{"promql": ""}]}]}, "query 1"),
    ({"panels": [{"type": "stat", "title": "x", "w": 13, "queries": [{"promql": "up"}]}]}, "width"),
    ({"panels": [{"type": "stat", "title": "x", "unit": "parsecs", "queries": [{"promql": "up"}]}]}, "unit"),
    ({"panels": [{"id": "a", "type": "text", "title": "x"}, {"id": "a", "type": "text", "title": "y"}]}, "repeated id"),
    ({"panels": [{"type": "text", "title": "x"}] * 61}, "at most 60"),
    ({"variables": [{"name": "bad label!"}]}, "label name"),
])
def test_dashboard_validation(aws, change, why):
    s, e = dash_call("acme", "POST", body={**DASH, **change})
    assert s == 400 and why in e["error"], (change, e)


def test_builtin_dashboards_can_be_cloned():
    """The web app's built-in dashboards are cloned by POSTing them: their panels must pass validation."""
    import re
    import dashboards
    src = open(os.path.join(os.path.dirname(__file__), "..", "web", "src", "builtinDashboards.ts")).read()
    ids = re.findall(r'(?:\bid: |text\()"([^"]+)"', src)
    panel_ids = [i for i in ids if not i.startswith("builtin-")]
    assert len(panel_ids) > 20 and all(dashboards._PANEL_ID.match(i) for i in panel_ids), panel_ids
    assert set(re.findall(r'type: "(\w+)"', src)) <= dashboards.PANEL_TYPES
    assert set(re.findall(r'unit: "([^"]*)"', src)) <= dashboards.UNITS
