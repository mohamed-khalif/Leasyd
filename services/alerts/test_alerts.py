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
sys.path[:0] = [HERE, os.path.join(HERE, "..", "synthetics"), os.path.join(HERE, "..", "ingest"),
                os.path.join(HERE, "..", "tenants")]
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
                     ({"type": "teams", "name": "x", "url": "https://evil.example.com/workflows/x"}, "Teams Workflows"),
                     ({"type": "pager", "name": "x"}, "email, slack, teams or webhook")]:
        s, e = call("acme", "POST", "channels", bad)
        assert s == 400 and why in e["error"], (bad, e)
    s, teams = call("acme", "POST", "channels", {"type": "teams", "name": "Ops (Teams)",
                                                 "url": "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SECRET"})
    assert s == 201 and teams["type"] == "teams" and "SECRET" not in json.dumps(teams)
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
    assert call("acme", "POST", "rules", {**good, "investigation_channels": ["nope00000000"]})[0] == 400
    s, r2 = call("acme", "POST", "rules", {**good, "name": "Two", "investigation_channels": [ch["id"]]})
    assert s == 201 and r2["investigation_channels"] == [ch["id"]]
    assert call("globex", "GET", "rules")[1]["items"] == []
    assert call("globex", "PUT", f"rules/{r['id']}", {"failures": 1})[0] == 404
    assert "Checkout down" in call("acme", "DELETE", f"channels/{ch['id']}")[1]["error"]


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
    prom.series = [({"service_name": "checkout"}, 1.0), ({"service_name": "cart"}, 0.5)]
    assert ev(3) == {"critical": 0, "degraded": 0, "ok": 2}                    # both back to normal: one message
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


def test_failed_evaluations_never_change_alerts_and_the_third_in_a_row_is_told_once(aws, monkeypatch):
    from datetime import datetime, timedelta, timezone
    prom, sent = FakeProm(), []
    monkeypatch.setattr(alerts, "_invoke", prom)
    monkeypatch.setattr(alerts, "notify", lambda tenant, channel, msg: sent.append(msg) and None)
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    rule = query_rule("acme", ch, degraded=None)
    item = alerts._plain(alerts._get(f"alert#acme#{rule['id']}", "acme"))
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    ev = lambda m: alerts._safe_eval_query(item, t0 + timedelta(minutes=m))  # noqa: E731
    prom.series = [({"service_name": "checkout"}, 9.0)]
    ev(0)
    assert len(sent) == 1 and "FIRING" in sent[0]["title"]
    sent.clear()
    prom.error = "the query engine is unavailable"
    for m in (1, 2):
        ev(m)
    assert not sent                                                            # 2 failures: not yet
    assert call("acme", "GET", "rules")[1]["items"][0]["firing"] == ["service_name=checkout (critical)"]   # still firing
    assert call("acme", "GET", "rules")[1]["items"][0]["eval_failing"] is False
    ev(3)
    assert len(sent) == 1 and "CAN'T EVALUATE" in sent[0]["title"] and sent[0]["payload"]["state"] == "eval_failed"
    assert call("acme", "GET", "rules")[1]["items"][0]["eval_failing"] is True
    assert "firing ones stay firing" in sent[0]["text"]
    for m in (4, 5, 6):
        ev(m)
    assert len(sent) == 1                                                      # told once, not every minute
    prom.error = None
    ev(7)                                                                      # works again: still firing, nothing new to say
    assert call("acme", "GET", "rules")[1]["items"][0]["eval_failing"] is False
    assert len(sent) == 1 and call("acme", "GET", "rules")[1]["items"][0]["firing"] == ["service_name=checkout (critical)"]
    prom.error = "boom"
    for m in (8, 9):
        ev(m)
    assert len(sent) == 1                                                      # the count starts again after a success
    ev(10)
    assert len(sent) == 2 and "CAN'T EVALUATE" in sent[1]["title"]


def test_a_failing_slo_rule_keeps_its_state_and_is_told_once(aws, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notify", lambda tenant, channel, msg: sent.append(msg) and None)
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    s, rule = call("acme", "POST", "rules", {"name": "Budget", "type": "slo_burn", "slo": "sssstttttttt", "burn_rate": 10, "channels": [ch]})
    numbers = {"week": (995, 1000), "hour": (50, 60), "error": None}
    def fake_query(q):
        if numbers["error"]:
            raise RuntimeError(numbers["error"])
        recent = alerts.datetime.fromisoformat(q["start"].replace("Z", "+00:00")) > alerts.datetime.now(alerts.timezone.utc) - alerts.timedelta(hours=2)
        good, total = numbers["hour" if recent else "week"]
        return {"columns": ["sum(value)", "count"], "rows": [[good, total]]}
    monkeypatch.setattr(alerts, "_query", fake_query)
    alerts.evaluate()
    assert len(sent) == 1 and "FIRING" in sent[0]["title"]
    numbers["error"] = "query failed: timeout"
    for _ in range(2):
        alerts.evaluate()
    assert len(sent) == 1
    alerts.evaluate()
    assert len(sent) == 2 and "CAN'T EVALUATE" in sent[1]["title"]
    assert alerts._state("acme", rule["id"], "sssstttttttt").get("firing")   # still firing
    assert "query failed" not in sent[1]["text"]                               # an internal error isn't passed on


def test_missing_data_never_resolves_a_firing_alert(aws, monkeypatch):
    from datetime import datetime, timedelta, timezone
    prom, sent = FakeProm(), []
    monkeypatch.setattr(alerts, "_invoke", prom)
    monkeypatch.setattr(alerts, "notify", lambda tenant, channel, msg: sent.append(msg) and None)
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    rule = query_rule("acme", ch, degraded=None)
    item = alerts._plain(alerts._get(f"alert#acme#{rule['id']}", "acme"))
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    ev = lambda m: alerts.evaluate_query_rule(item, t0 + timedelta(minutes=m))  # noqa: E731
    prom.series = [({"service_name": "checkout"}, 9.0), ({"service_name": "cart"}, 4.0)]
    ev(0)
    assert len(sent) == 1 and "checkout (critical)" in sent[0]["title"]
    sent.clear()
    prom.series = []                                                           # the data stopped: nothing comes back
    for m in (1, 2):
        assert ev(m) == {"critical": 0, "degraded": 0, "ok": 0}
    assert not sent                                                            # 2 checks without data: not yet
    listing = call("acme", "GET", "rules")[1]["items"][0]
    assert listing["firing"] == ["service_name=checkout (critical)"] and listing["no_data"] == ["service_name=checkout"]
    ev(3)
    assert len(sent) == 1 and "NO DATA" in sent[0]["title"] and "service_name=checkout" in sent[0]["title"]
    assert sent[0]["payload"]["state"] == "no_data" and "or vector(0)" in sent[0]["text"]
    for m in (4, 5):
        ev(m)
    assert len(sent) == 1                                                      # told once
    assert not alerts._state("acme", rule["id"], alerts._series_id({"service_name": "cart"}))   # cart never fired: forgotten quietly
    prom.series = [({"service_name": "checkout"}, float("nan"))]               # NaN is no data too
    ev(6)
    assert len(sent) == 1 and call("acme", "GET", "rules")[1]["items"][0]["firing"] == ["service_name=checkout (critical)"]
    prom.series = [({"service_name": "checkout"}, 9.5)]                        # data is back, still critical: no longer "no data"
    ev(7)
    assert len(sent) == 1 and call("acme", "GET", "rules")[1]["items"][0]["no_data"] == []
    prom.series = [({"service_name": "checkout"}, 1.0)]                        # a value within limits resolves it
    ev(8)
    assert len(sent) == 2 and "RESOLVED" in sent[1]["title"]
    assert call("acme", "GET", "rules")[1]["items"][0]["firing"] == []


def test_an_slo_with_no_runs_stays_firing_and_is_told_once(aws, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notify", lambda tenant, channel, msg: sent.append(msg) and None)
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    s, rule = call("acme", "POST", "rules", {"name": "Budget", "type": "slo_burn", "slo": "sssstttttttt", "burn_rate": 10, "channels": [ch]})
    numbers = {"week": (995, 1000), "hour": (50, 60)}
    def fake_query(q):
        recent = alerts.datetime.fromisoformat(q["start"].replace("Z", "+00:00")) > alerts.datetime.now(alerts.timezone.utc) - alerts.timedelta(hours=2)
        good, total = numbers["hour" if recent else "week"]
        return {"columns": ["sum(value)", "count"], "rows": [[good, total]]}
    monkeypatch.setattr(alerts, "_query", fake_query)
    alerts.evaluate()
    assert len(sent) == 1 and "FIRING" in sent[0]["title"]
    numbers.update(week=(0, 0), hour=(0, 0))                                   # no runs at all (the check stopped)
    for _ in range(2):
        alerts.evaluate()
    assert len(sent) == 1                                                      # not resolved, not yet told
    alerts.evaluate()
    assert len(sent) == 2 and "NO DATA" in sent[1]["title"] and sent[1]["payload"]["state"] == "no_data"
    alerts.evaluate()
    assert len(sent) == 2 and alerts._state("acme", rule["id"], "sssstttttttt").get("firing")
    assert call("acme", "GET", "rules")[1]["items"][0]["no_data"] == ["Checkout up"]
    numbers.update(week=(998, 1000), hour=(60, 60))                            # runs again, within limits
    alerts.evaluate()
    assert len(sent) == 3 and "RESOLVED" in sent[2]["title"]


def test_a_check_failing_alert_whose_check_stopped_running_says_no_data_once(aws, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notify", lambda tenant, channel, msg: sent.append(msg) and None)
    monkeypatch.setattr(alerts, "slo_status", lambda *a: {"attainment": None, "budget_left": None, "burn1h": None})
    ch = call("acme", "POST", "channels", {"type": "slack", "name": "#ops", "url": "https://hooks.slack.com/services/T0/B0/x"})[1]["id"]
    s, rule = call("acme", "POST", "rules", {"name": "Checkout down", "type": "check_failing", "checks": ["aaaabbbbcccc"], "failures": 1, "channels": [ch]})
    alerts.on_result({"tenant": "acme", "check_id": "aaaabbbbcccc", "check_name": "Checkout API", "ok": False, "failure": "503"})
    assert len(sent) == 1 and "FIRING" in sent[0]["title"]
    alerts.evaluate()
    assert len(sent) == 1                                                      # it ran just now: fine
    t = boto3.resource("dynamodb").Table("obs-tenants")
    st = t.get_item(Key={"pk": f"astate#acme#{rule['id']}#aaaabbbbcccc"})["Item"]
    st["updated_at"] = "2026-01-01T00:00:00Z"                                 # no run since (every 5 minutes: 3 missed long ago)
    t.put_item(Item=st)
    alerts.evaluate()
    assert len(sent) == 2 and "NO DATA" in sent[1]["title"] and "Checkout API" in sent[1]["title"]
    alerts.evaluate()
    assert len(sent) == 2                                                      # told once
    assert call("acme", "GET", "rules")[1]["items"][0]["firing"] == ["aaaabbbbcccc"]
    alerts.on_result({"tenant": "acme", "check_id": "aaaabbbbcccc", "check_name": "Checkout API", "ok": True})
    assert len(sent) == 3 and "RESOLVED" in sent[2]["title"]                   # a passing run resolves it as before


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


def test_an_account_users_address_gets_alerts_at_once_by_ses(aws, monkeypatch):
    # The SNS confirmation link held every alert email back until clicked (QA 2026-10-06). An
    # address that signs in to the account is already proven theirs: Leasyd sends it directly.
    sent = []
    monkeypatch.setattr(alerts.emails, "EMAIL_FROM", "Leasyd <no-reply@app.leasyd.com>")
    monkeypatch.setattr(alerts.emails, "send", lambda to, subject, paragraphs, button=None, **kw: sent.append((to, subject, paragraphs, button)))
    table = boto3.resource("dynamodb").Table("obs-tenants")
    table.put_item(Item={"pk": "user#ana@acme.io", "tenant": "acme", "status": "active", "email": "ana@acme.io"})
    table.put_item(Item={"pk": "user#bo@globex.io", "tenant": "globex", "status": "active", "email": "bo@globex.io"})
    s, ch = call("acme", "POST", "channels", {"type": "email", "name": "Ana", "email": "Ana@acme.io"})
    assert s == 201 and ch["status"] == "confirmed" and ch["delivery"] == "ses"
    assert not boto3.client("sns").list_topics()["Topics"]                       # no SNS topic, no AWS email
    s, other = call("acme", "POST", "channels", {"type": "email", "name": "Bo", "email": "bo@globex.io"})
    assert s == 201 and "delivery" not in other                                # another account's user: SNS, confirms
    assert len(boto3.client("sns").list_topics()["Topics"]) == 1
    s, out = call("acme", "POST", f"channels/{ch['id']}/test", {})
    assert s == 200 and out["sent"] is True
    [(to, subject, paragraphs, button)] = sent
    assert to == "ana@acme.io" and "Test alert" in subject and button[0] == "Open in Leasyd"


def test_a_viewer_can_look_but_not_change_or_send(aws):
    """The public live demo's login (role viewer): reads and rule previews work; changing channels,
    rules or dashboards, and test notifications, are refused."""
    boto3.resource("dynamodb").Table("obs-tenants").put_item(
        Item={"pk": "user#ana@demo.io", "tenant": "demo", "role": "viewer", "status": "active"})
    refused = (403, {"error": alerts.VIEWER_REFUSED})
    assert call("demo", "GET", "rules")[0] == 200 and call("demo", "GET", "channels")[0] == 200
    assert call("demo", "POST", "rules/preview", {"query": "x"})[0] != 403
    for method, path in [("POST", "channels"), ("POST", "channels/abc/test"), ("POST", "rules"),
                         ("PUT", "rules/abc"), ("DELETE", "rules/abc"), ("DELETE", "channels/abc")]:
        assert call("demo", method, path, {"name": "x"}) == refused
    dash = {"httpMethod": "POST", "resource": "/v1/app/dashboards", "pathParameters": None, "body": json.dumps({"name": "x"}),
            "requestContext": {"authorizer": {"claims": {"custom:tenant": "demo", "email": "ana@demo.io"}}}}
    r = alerts.api(dash)
    assert (r["statusCode"], json.loads(r["body"])) == refused
    assert alerts.api({**dash, "httpMethod": "GET", "body": None})["statusCode"] == 200
    assert call("acme", "POST", "channels", {"name": "x", "type": "email", "target": "ana@acme.io"})[0] != 403


def test_a_firing_starts_an_investigation_unless_the_rule_turns_it_off(aws, hook, monkeypatch):
    started = []
    monkeypatch.setattr(alerts, "INVESTIGATOR_FUNCTION", "obs-investigator")
    monkeypatch.setitem(alerts._clients, "lambda", type("L", (), {"invoke": lambda self, **kw: started.append(kw)})())
    ch = webhook_channel("acme", hook)
    s, on = call("acme", "POST", "rules", {"name": "Checkout down", "type": "check_failing", "checks": ["*"], "failures": 1, "channels": [ch["id"]]})
    assert on["investigate"] is True                                           # on by default
    run = lambda ok: alerts.on_result({"tenant": "acme", "check_id": "aaaabbbbcccc", "check_name": "Checkout API", "ok": ok,  # noqa: E731
                                       "failure": None if ok else "status 503"})
    run(False)
    run(True)                                                                  # resolving starts nothing
    assert len(started) == 1 and started[0]["FunctionName"] == "obs-investigator" and started[0]["InvocationType"] == "Event"
    sent = json.loads(started[0]["Payload"])
    assert sent["tenant"] == "acme" and sent["alert"]["state"] == "firing" and sent["alert"]["rule"]["id"] == on["id"]
    call("acme", "PUT", f"rules/{on['id']}", {**{k: on[k] for k in ("name", "type", "checks", "failures", "channels")}, "investigate": False})
    run(False)
    assert len(started) == 1                                                   # switched off: still alerted, not investigated

    def broken(**kw):
        raise ConnectionError("lambda down")
    monkeypatch.setitem(alerts._clients, "lambda", type("L", (), {"invoke": lambda self, **kw: broken(**kw)})())
    alerts.investigate("acme", {"state": "firing"})                            # never raises: the alert already went out
