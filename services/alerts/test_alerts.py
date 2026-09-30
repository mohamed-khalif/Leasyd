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
                        ({"type": "slo_burn", "slo": "sssstttttttt"}, "burn_rate, budget_below"), ({"type": "x"}, "check_failing or slo_burn")]:
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
