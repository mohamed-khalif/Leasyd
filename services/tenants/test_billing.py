"""Billing (billing.py) with a fake Stripe: settings from SSM, checkout and portal, signed webhooks
upgrading and ending subscriptions, and the daily usage report."""

import hashlib
import hmac
import io
import json
import time
import urllib.error
import urllib.parse

import boto3
import pytest

import billing
from test_account import acc, app  # noqa: F401  (fixtures)
from test_admin import CTX, adm, mail  # noqa: F401

SECRET = "whsec_test"
PRICES = {m: f"price_{m}" for m in billing.METERS}
EVENTS = {m: f"leasyd_{m}" for m in billing.METERS}


class FakeStripe:
    """Records each API call; answers like Stripe for the few endpoints billing.py uses."""

    def __init__(self):
        self.calls, self.fail = [], None

    def __call__(self, req, timeout=None):
        path = req.full_url.replace(billing.API, "")
        form = dict(urllib.parse.parse_qsl(req.data.decode())) if req.data else {}
        self.calls.append((req.get_method(), path, form, dict(req.header_items())))
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(json.dumps({"error": {"message": self.fail}}).encode()))
        out = {"/checkout/sessions": {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"},
               "/billing_portal/sessions": {"id": "bps_1", "url": "https://billing.stripe.com/p/session/1"},
               "/billing/meter_events": {"object": "billing.meter_event"},
               "/subscriptions/sub_1": {"id": "sub_1", "status": "canceled"}}[path]
        return type("R", (), {"read": lambda self: json.dumps(out).encode(), "__enter__": lambda self: self,
                              "__exit__": lambda self, *a: None})()


@pytest.fixture
def stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(billing.urllib.request, "urlopen", fake)
    billing._settings.clear()
    billing._settings.update(secret_key="sk_test_x", webhook_secret=SECRET, prices=PRICES, events=EVENTS, loaded=time.time())
    yield fake
    billing._settings.clear()


def signed(event, t=None, secret=SECRET):
    raw = json.dumps(event).encode()
    t = int(t or time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, f"t={t},v1={sig}"


def webhook(acc, event, **kw):  # noqa: F811
    import base64
    raw, header = signed(event, **kw)
    out = acc.handler({"resource": "/v1/stripe/webhook", "httpMethod": "POST", "isBase64Encoded": True,
                       "headers": {"Stripe-Signature": header}, "body": base64.b64encode(raw).decode()}, None)
    return out["statusCode"], json.loads(out["body"])


def trial(adm, acc, tenant="acme", email="ana@acme.com"):  # noqa: F811
    """A self-service sign-up (a free trial) whose tenant is named tenant."""
    acc.signup({"email": email, "company": tenant}, "203.0.113.7")
    adm.handler(acc.started.pop(), CTX)
    return tenant


def record(tenant):
    return boto3.resource("dynamodb").Table("obs-tenants").get_item(Key={"pk": f"tenant#{tenant}"})["Item"]


# ------------------------------------------------------------------ settings and the API

def test_settings_come_from_ssm_and_billing_is_off_without_them(adm):  # noqa: F811
    billing._settings.clear()
    assert billing.settings() == {}                     # nothing in SSM: off, never an error
    billing._settings.clear()
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name="/obs/stripe/secret_key", Value="sk_test_1", Type="SecureString")
    ssm.put_parameter(Name="/obs/stripe/webhook_secret", Value="whsec_1", Type="SecureString")
    ssm.put_parameter(Name="/obs/stripe/config", Value=json.dumps({"prices": PRICES, "events": EVENTS}), Type="String")
    s = billing.settings()
    assert s["secret_key"] == "sk_test_1" and s["webhook_secret"] == "whsec_1" and s["prices"] == PRICES
    billing._settings.clear()


def test_form_encoding_is_stripes():
    assert billing._encode({"a": 1, "line_items": [{"price": "p1"}, {"price": "p2"}], "m": {"tenant": "t"},
                            "x": None, "b": True}) == [
        ("a", "1"), ("line_items[0][price]", "p1"), ("line_items[1][price]", "p2"), ("m[tenant]", "t"), ("b", "true")]


# ------------------------------------------------------------------ checkout and portal

def test_an_owner_starts_a_subscription_through_checkout(adm, acc, stripe):  # noqa: F811
    tenant = trial(adm, acc)
    status, me = app(acc, "ana@acme.com", tenant)
    assert me["billing"] == {"enabled": True, "status": None, "has_customer": False, "started_at": None}
    status, out = app(acc, "ana@acme.com", tenant, "POST", "billing/checkout")
    assert status == 200 and out["url"].startswith("https://checkout.stripe.com/")
    method, path, form, headers = stripe.calls[-1]
    assert (method, path) == ("POST", "/checkout/sessions") and headers["Authorization"] == "Bearer sk_test_x"
    assert form["mode"] == "subscription" and form["client_reference_id"] == tenant
    assert form["metadata[tenant]"] == tenant and form["subscription_data[metadata][tenant]"] == tenant
    assert form["customer_email"] == "ana@acme.com" and "/#/settings?billing=done" in form["success_url"]
    assert sorted(v for k, v in form.items() if k.startswith("line_items")) == sorted(PRICES.values())
    # No customer yet: nothing to manage. A member may not start billing.
    assert app(acc, "ana@acme.com", tenant, "POST", "billing/portal")[0] == 400
    adm.handler({"action": "invite-user", "tenant": tenant, "email": "bo@acme.com", "role": "member", "send_email": False}, CTX)
    assert app(acc, "bo@acme.com", tenant, "POST", "billing/checkout")[0] == 403


def test_checkout_says_when_billing_is_not_set_up(adm, acc):  # noqa: F811
    billing._settings.clear()
    billing._settings["loaded"] = time.time()
    tenant = trial(adm, acc)
    status, out = app(acc, "ana@acme.com", tenant, "POST", "billing/checkout")
    assert status == 503 and "isn't set up" in out["error"]
    assert app(acc, "ana@acme.com", tenant)[1]["billing"]["enabled"] is False
    billing._settings.clear()


def test_stripe_errors_are_answered_not_raised(adm, acc, stripe):  # noqa: F811
    tenant = trial(adm, acc)
    stripe.fail = "No such price: 'price_x'"
    status, out = app(acc, "ana@acme.com", tenant, "POST", "billing/checkout")
    assert status == 502 and out["error"] == "Stripe: No such price: 'price_x'"


# ------------------------------------------------------------------ webhooks

def completed(tenant, sub="sub_1", customer="cus_1"):
    return {"id": "evt_1", "type": "checkout.session.completed",
            "data": {"object": {"mode": "subscription", "client_reference_id": tenant, "metadata": {"tenant": tenant},
                                "customer": customer, "subscription": sub, "customer_details": {"email": "ana@acme.com"}}}}


def sub_event(tenant, kind, status, sub="sub_1"):
    return {"id": "evt_2", "type": f"customer.subscription.{kind}",
            "data": {"object": {"id": sub, "status": status, "metadata": {"tenant": tenant}}}}


def test_a_paid_checkout_upgrades_the_trial(adm, acc, stripe):  # noqa: F811
    tenant = trial(adm, acc)
    key = app(acc, "ana@acme.com", tenant, "POST", "keys", {"scope": "ingest"})[1]
    assert record(tenant)["plan"] == "free" and "trial_ends_at" in record(tenant)
    assert webhook(acc, completed(tenant)) == (200, {"received": True})
    rec = record(tenant)
    assert rec["plan"] == "standard" and "trial_ends_at" not in rec and "daily_cap_bytes" not in rec
    assert (rec["billing_status"], rec["stripe_customer_id"], rec["stripe_subscription_id"]) == ("active", "cus_1", "sub_1")
    assert [k["plan"] for k in adm._keys(tenant) if k["api_key_id"] == key["key_id"]] == ["standard"]
    me = app(acc, "ana@acme.com", tenant)[1]
    assert me["billing"]["status"] == "active" and me["billing"]["has_customer"] and me["plan"] == "standard"
    # Now the portal opens for the customer; a second checkout is refused.
    assert app(acc, "ana@acme.com", tenant, "POST", "billing/portal")[1]["url"].startswith("https://billing.stripe.com/")
    assert stripe.calls[-1][2] == {"customer": "cus_1", "return_url": "https://app.leasyd.com/#/settings"}   # no portal configuration set
    assert app(acc, "ana@acme.com", tenant, "POST", "billing/checkout")[0] == 409


def test_subscription_changes_and_its_end(adm, acc, stripe):  # noqa: F811
    tenant = trial(adm, acc)
    webhook(acc, completed(tenant))
    webhook(acc, sub_event(tenant, "updated", "past_due"))
    assert record(tenant)["billing_status"] == "past_due"
    webhook(acc, sub_event(tenant, "deleted", "canceled", sub="sub_old"))      # an older subscription: ignored
    assert record(tenant)["billing_status"] == "past_due"
    webhook(acc, sub_event(tenant, "deleted", "canceled"))
    assert record(tenant)["billing_status"] == "canceled"                     # ingest refuses its data now
    # Subscribing again (same customer) starts the new subscription.
    assert app(acc, "ana@acme.com", tenant, "POST", "billing/checkout")[0] == 200
    assert stripe.calls[-1][2]["customer"] == "cus_1"
    webhook(acc, completed(tenant, sub="sub_2"))
    assert (record(tenant)["billing_status"], record(tenant)["stripe_subscription_id"]) == ("active", "sub_2")


def test_webhooks_must_be_signed_and_recent(adm, acc, stripe):  # noqa: F811
    tenant = trial(adm, acc)
    assert webhook(acc, completed(tenant), secret="whsec_wrong")[0] == 400
    assert webhook(acc, completed(tenant), t=time.time() - 3600) == (400, {"error": "signature too old"})
    assert record(tenant)["plan"] == "free"
    # Other events are acknowledged and do nothing; so are events of unknown tenants.
    assert webhook(acc, {"id": "evt_3", "type": "invoice.paid", "data": {"object": {}}})[0] == 200
    assert webhook(acc, completed("nobody-here"))[0] == 200


# ------------------------------------------------------------------ usage

def test_day_usage_reads_the_meter_and_stored_records():
    meter = {"in_logs": 1000, "dropped_logs": 400, "in_traces": 50, "checks_http": 1440, "checks_browser": 24}
    usage = [{"signal": "logs", "records": 400}, {"signal": "logs", "records": 200}, {"signal": "traces", "records": 50}]
    assert billing.day_usage(meter, usage) == {
        "ingest_traces": 50, "ingest_logs": 1000, "ingest_metrics": 0, "stored_traces": 50, "stored_logs": 600,
        "stored_metrics": 0, "check_runs_http": 1440, "check_runs_browser": 24}


def test_each_subscribed_day_is_reported_once(adm, acc, stripe, monkeypatch):  # noqa: F811
    from datetime import datetime, timezone
    tenant = trial(adm, acc)
    webhook(acc, completed(tenant))
    other = trial(adm, acc, "globex", "gil@globex.com")                     # still on the trial: not billed
    monkeypatch.setattr(adm, "_now", lambda: datetime(2026, 10, 6, 2, 5, tzinfo=timezone.utc))
    t = boto3.resource("dynamodb").Table("obs-tenants")
    t.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET billing_started_at = :d",
                  ExpressionAttributeValues={":d": "2026-10-04T12:00:00Z"})
    for who in (tenant, other):
        for day in ("2026-10-03", "2026-10-04", "2026-10-05"):
            t.put_item(Item={"pk": f"meter#{who}#{day}", "in_logs": 100, "checks_http": 10})
    boto3.resource("dynamodb").Table("obs-usage").put_item(
        Item={"tenant": tenant, "sk": "2026-10-05#logs#00", "dt": "2026-10-05", "signal": "logs", "records": 80})
    out = adm.handler({"action": "report-usage"}, CTX)
    assert out["days"] == ["2026-10-03", "2026-10-04", "2026-10-05"]
    assert out["reported"] == {tenant: {"2026-10-04": {"ingest_logs": 100, "check_runs_http": 10},
                                        "2026-10-05": {"ingest_logs": 100, "stored_logs": 80, "check_runs_http": 10}}}
    sent = [c[2] for c in stripe.calls if c[1] == "/billing/meter_events"]
    assert len(sent) == 5 and all(s["payload[stripe_customer_id]"] == "cus_1" for s in sent)
    assert {s["identifier"] for s in sent} >= {f"{tenant}:2026-10-05:stored_logs"}
    assert {s["timestamp"] for s in sent} == {str(int(datetime(2026, 10, 4, 23, 59, 59, tzinfo=timezone.utc).timestamp())),
                                             str(int(datetime(2026, 10, 5, 23, 59, 59, tzinfo=timezone.utc).timestamp()))}
    # The next run sends nothing again; a failed day is retried the next time.
    assert adm.handler({"action": "report-usage"}, CTX)["reported"] == {}
    t.delete_item(Key={"pk": f"billed#{tenant}#2026-10-05"})
    stripe.fail = "Stripe is down"
    out = adm.handler({"action": "report-usage"}, CTX)
    assert out["failed"] == {tenant: {"2026-10-05": "Stripe: Stripe is down"}}
    assert "Item" not in t.get_item(Key={"pk": f"billed#{tenant}#2026-10-05"})


def test_deleting_a_tenant_cancels_its_subscription(adm, acc, stripe):  # noqa: F811
    tenant = trial(adm, acc)
    webhook(acc, completed(tenant))
    adm.handler({"action": "delete", "tenant": tenant}, CTX)
    method, path, form, _ = stripe.calls[-1]
    assert (method, path, form) == ("DELETE", "/subscriptions/sub_1", {"invoice_now": "true"})
