"""The AWS adapters and the Lambda handlers, against moto: an alert or webhook starts an investigation,
the worker runs it from the real queue message, and every route keeps to its own tenant."""
import base64
import importlib
import json
import os

import boto3
import pytest
from moto import mock_aws

os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                  BUCKET="obs-data-test", TENANTS_TABLE="obs-tenants")
os.environ.pop("AWS_SESSION_TOKEN", None)

ALERT = {"tenant": "acme", "state": "firing", "rule": {"id": "r1", "name": "Checkout errors", "type": "query"},
         "subject": "checkout", "detail": "error rate 6.2% > 5%", "url": "https://app.leasyd.com/#/alerts", "time": "2026-10-30T02:14:05Z"}


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="obs-data-test")
        boto3.client("dynamodb").create_table(
            TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "tenant", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "by-tenant", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "tenant", "KeyType": "HASH"}, {"AttributeName": "pk", "KeyType": "RANGE"}]}])
        url = boto3.client("sqs").create_queue(QueueName="obs-investigations")["QueueUrl"]
        monkeypatch.setenv("QUEUE_URL", url)
        from handlers import deps, ingest, worker
        importlib.reload(deps)
        importlib.reload(ingest)
        importlib.reload(worker)
        table = boto3.resource("dynamodb").Table("obs-tenants")
        for email, tenant, role in [("ana@acme.io", "acme", "owner"), ("bo@acme.io", "acme", "member"), ("cy@globex.io", "globex", "owner")]:
            table.put_item(Item={"pk": f"user#{email}", "tenant": tenant, "role": role, "status": "active"})
        yield type("Env", (), {"ingest": ingest, "worker": worker, "deps": deps, "url": url, "table": table})


def drain(env):
    """Hands every queued message to the worker, as SQS's Lambda trigger would. -> the messages."""
    sqs, seen = boto3.client("sqs"), []
    while True:
        got = sqs.receive_message(QueueUrl=env.url, MaxNumberOfMessages=10, AttributeNames=["All"]).get("Messages", [])
        if not got:
            return seen
        out = env.worker.handler({"Records": [{"messageId": m["MessageId"], "body": m["Body"], "attributes": m["Attributes"]} for m in got]})
        assert out == {"batchItemFailures": []}
        for m in got:
            seen.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=env.url, ReceiptHandle=m["ReceiptHandle"])


def app(env, method, path="", tenant="acme", user="ana@acme.io"):
    out = env.ingest.handler({"resource": "/v1/app/investigations/{proxy+}" if path else "/v1/app/investigations", "httpMethod": method,
                              "pathParameters": {"proxy": path} if path else None,
                              "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": user}}}})
    return out["statusCode"], json.loads(out["body"])


def hook(env, token, body, b64=True):
    raw = json.dumps(body)
    out = env.ingest.handler({"resource": "/v1/investigations/webhook/{token}", "httpMethod": "POST", "pathParameters": {"token": token},
                              "body": base64.b64encode(raw.encode()).decode() if b64 else raw, "isBase64Encoded": b64})
    return out["statusCode"], json.loads(out["body"])


def test_an_alert_firing_is_investigated_in_the_background(env):
    out = env.ingest.handler({"alert": ALERT, "tenant": "acme"})
    assert out["status"] == "queued" and not out["joined"]
    again = env.ingest.handler({"alert": ALERT, "tenant": "acme"})       # the same alert again: joins it
    assert again == {"investigation": out["investigation"], "joined": True, "status": "queued"}
    assert env.ingest.handler({"alert": {**ALERT, "state": "resolved"}, "tenant": "acme"}) == {"ignored": "alert state resolved"}
    msgs = drain(env)
    assert msgs == [{"run": {"tenant": "acme", "id": out["investigation"]}}, {"deliver": {"tenant": "acme", "id": out["investigation"]}}]
    status, inv = app(env, "GET", out["investigation"])
    assert status == 200 and inv["status"] == "completed" and inv["result"]["root_cause"] == "not sure"
    assert [e["type"] for e in inv["events"]] == ["triggered", "trigger_joined", "running", "completed"]
    assert inv["trigger"]["rule_id"] == "r1" and inv["result"]["safe_to_auto_act"] is False
    status, listed = app(env, "GET")
    assert [(i["id"], i["status"], i["title"]) for i in listed["investigations"]] == [(out["investigation"], "completed", "Checkout errors: checkout")]


def test_tenants_never_see_each_others_investigations(env):
    out = env.ingest.handler({"alert": ALERT, "tenant": "acme"})
    assert app(env, "GET", out["investigation"], tenant="globex", user="cy@globex.io")[0] == 404
    assert app(env, "GET", tenant="globex", user="cy@globex.io")[1] == {"investigations": []}
    assert app(env, "GET", "not-an-id")[0] == 404
    assert env.ingest.handler({"resource": "/v1/app/investigations", "httpMethod": "GET"})["statusCode"] == 401


def test_the_webhook_url_belongs_to_one_tenant_and_a_new_one_retires_the_old(env):
    assert app(env, "POST", "webhook-token", user="bo@acme.io")[0] == 403         # members can't
    status, made = app(env, "POST", "webhook-token")
    assert status == 201 and made["url"].startswith("https://ingest.leasyd.com/v1/investigations/webhook/")
    token = made["url"].rsplit("/", 1)[1]
    status, out = hook(env, token, {"title": "Queue backing up", "service": "orders"})
    assert status == 202 and out["status"] == "queued"
    status, inv = app(env, "GET", out["investigation"])
    assert inv["trigger"]["source"] == "generic" and inv["tenant"] == "acme"
    assert app(env, "GET", out["investigation"], tenant="globex", user="cy@globex.io")[0] == 404
    assert hook(env, "x" * 32, {"title": "y"})[0] == 404                           # unknown token: nothing said
    assert hook(env, "../../etc", {"title": "y"})[0] == 404
    assert hook(env, token, {"severity": "panic"}, b64=False)[0] == 400
    assert hook(env, token, {"Type": "Notification", "Message": json.dumps({"AlarmName": "a", "NewStateValue": "OK"})}) == (202, {"ignored": "alarm state OK"})
    new = app(env, "POST", "webhook-token")[1]["url"].rsplit("/", 1)[1]
    assert hook(env, token, {"title": "z"})[0] == 404 and hook(env, new, {"title": "z"})[0] == 202


def test_sns_subscription_is_confirmed_only_at_an_aws_address(env, monkeypatch):
    token = app(env, "POST", "webhook-token")[1]["url"].rsplit("/", 1)[1]
    opened = []

    class Resp:
        def read(self):
            return b"<ConfirmSubscriptionResponse/>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(env.ingest.urllib.request, "urlopen", lambda url, timeout: (opened.append(url), Resp())[1])
    ok = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=t&Token=k"
    assert hook(env, token, {"Type": "SubscriptionConfirmation", "SubscribeURL": ok}) == (200, {"confirmed": True})
    assert hook(env, token, {"Type": "SubscriptionConfirmation", "SubscribeURL": "https://evil.example/?a"})[0] == 400
    assert opened == [ok]


def test_over_the_cap_a_skipped_investigation_is_still_queued_for_delivery(env, monkeypatch):
    monkeypatch.setattr(env.deps, "CAP", 1)
    first = env.ingest.handler({"alert": ALERT, "tenant": "acme"})
    second = env.ingest.handler({"alert": {**ALERT, "subject": "cart"}, "tenant": "acme"})
    assert first["status"] == "queued" and second["status"] == "skipped"
    assert {"deliver": {"tenant": "acme", "id": second["investigation"]}} in drain(env)
    assert app(env, "GET", second["investigation"])[1]["skip_reason"] == "rate_limit"


def test_a_failing_run_is_reported_for_retry_and_others_in_the_batch_are_not(env, monkeypatch):
    out = env.ingest.handler({"alert": ALERT, "tenant": "acme"})

    class Broken:
        name, versions = "broken", {}

        def investigate(self, inv):
            raise RuntimeError("model unavailable")
    monkeypatch.setitem(env.deps._built, "agent", Broken())
    rec = lambda mid, body, n: {"messageId": mid, "body": json.dumps(body), "attributes": {"ApproximateReceiveCount": str(n)}}   # noqa: E731
    got = env.worker.handler({"Records": [rec("m1", {"run": {"tenant": "acme", "id": out["investigation"]}}, 1),
                                          rec("m2", {"deliver": {"tenant": "acme", "id": "x"}}, 1)]})
    assert got == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    got = env.worker.handler({"Records": [rec("m1", {"run": {"tenant": "acme", "id": out["investigation"]}}, 3)]})
    assert got == {"batchItemFailures": []}                                       # the last attempt: marked failed
    assert app(env, "GET", out["investigation"])[1]["status"] == "failed"


def test_store_refuses_a_stale_save(env):
    from domain.model import Investigation, Trigger
    from ports import StaleWrite
    store = env.deps.store()
    inv = Investigation(id="a" * 16, tenant="acme", status="queued", created_at="t", updated_at="t",
                        trigger=Trigger(source="generic", title="x", dedupe_key="k", started_at="t"))
    store.create(inv)
    with pytest.raises(StaleWrite):
        store.create(inv)
    one, two = store.get("acme", "a" * 16), store.get("acme", "a" * 16)
    one.log("t1", "note")
    store.save(one)
    two.log("t2", "note")
    with pytest.raises(StaleWrite):
        store.save(two)
    assert store.get("globex", "a" * 16) is None


def deploy(env, body, tenant="acme"):
    ctx = {"authorizer": {"tenant": tenant, "scope": "ingest"}} if tenant else {}
    out = env.ingest.handler({"resource": "/v1/deploys", "httpMethod": "POST", "requestContext": ctx,
                              "body": base64.b64encode(json.dumps(body).encode()).decode(), "isBase64Encoded": True})
    return out["statusCode"], json.loads(out["body"])


def deploys(env, tenant="acme", user="ana@acme.io", **q):
    out = env.ingest.handler({"resource": "/v1/app/deploys", "httpMethod": "GET", "queryStringParameters": q or None,
                              "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": user}}}})
    return out["statusCode"], json.loads(out["body"])


def test_ci_records_deploys_for_its_keys_tenant_only(env, monkeypatch):
    from domain.workflow import SystemClock
    monkeypatch.setattr(SystemClock, "now", lambda self: 1793327400.0)          # 2026-10-30T02:30:00Z
    body = {"service": "payment", "version": "3.1.0", "deployed_at": "2026-10-30T02:11:30Z", "url": "https://ci.example/runs/1"}
    status, out = deploy(env, body)
    assert status == 201 and out["deployed_at"] == "2026-10-30T02:11:30Z"
    assert deploy(env, body) == (201, out)                                           # posted twice: one deploy
    assert deploy(env, {**body, "service": "cart", "deployed_at": "2026-10-30T01:00:00Z"})[0] == 201
    assert deploy(env, {"service": "x"})[0] == 400 and deploy(env, body, tenant=None)[0] == 401
    status, listed = deploys(env)
    assert status == 200 and [(d["service"], d["version"]) for d in listed["deploys"]] == [("payment", "3.1.0"), ("cart", "3.1.0")]
    assert [d["service"] for d in deploys(env, service="cart")[1]["deploys"]] == ["cart"]
    assert deploys(env, tenant="globex", user="cy@globex.io")[1] == {"deploys": []}
    from adapters.aws import TenantDeploys
    seen = TenantDeploys(env.deps.store(), "acme").between("2026-10-30T02:00:00Z", "2026-10-30T02:30:00Z")
    assert [(d["service"], d["url"]) for d in seen] == [("payment", "https://ci.example/runs/1")]
    assert TenantDeploys(env.deps.store(), "globex").between("2026-10-29T00:00:00Z", "2026-10-31T00:00:00Z") == []
    row = env.table.get_item(Key={"pk": f"deploy#acme#2026-10-30T02:11:30Z#{out['deploy']}"})["Item"]
    assert row["tenant"] == "acme" and int(row["expires"]) == 1793327400 + 90 * 86400
