"""Billing with Stripe: usage-based subscriptions, paid by card through Stripe Checkout.

  checkout(tenant, email, record)  a Stripe Checkout page to add payment details and start the subscription
  portal(record)                   the Stripe customer portal (card, invoices, cancel)
  webhook(raw_body, header, ...)   Stripe's events: subscription started, changed, ended
  report(tenant, customer, day, usage)  a day's usage to Stripe's meters (obs-tenant-admin's
                                   report-usage runs it daily for each subscribed tenant)

Prices are Stripe metered prices on Stripe billing meters (infra/stripe-setup.py creates them and
stores their ids): what is reported each day is counted and charged on the monthly invoice.

  meter                 value reported for a day                    price
  ingest_<signal>       records received (meter#: in_<signal>)       per record (ingest)
  stored_<signal>       records stored (obs-usage, compacted)        per record (storage, 30 days)
  check_runs_http       scheduled HTTP check runs (meter#)           per run
  check_runs_browser    scheduled browser check runs (meter#)        per run

Settings live in SSM Parameter Store (never in the repo):
  /obs/stripe/secret_key       SecureString   the Stripe secret (or restricted) API key
  /obs/stripe/webhook_secret   SecureString   the webhook endpoint's signing secret (whsec_...)
  /obs/stripe/config           String (JSON)  {"prices": {meter: price id}, "events": {meter: event name},
                                              "portal_configuration": id}
Without them billing is off: checkout answers that it isn't set up, and nothing is reported.

Each tenant's Stripe customer and subscription are on its tenant# record (stripe_customer_id,
stripe_subscription_id, billing_status). A day is reported once per tenant (billed#<tenant>#<day>).
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

API = "https://api.stripe.com/v1"
PARAMS = os.environ.get("STRIPE_PARAMS", "/obs/stripe")
APP_URL = os.environ.get("APP_URL", "https://app.leasyd.com")
SIGNALS = ("traces", "logs", "metrics")
METERS = [f"ingest_{s}" for s in SIGNALS] + [f"stored_{s}" for s in SIGNALS] + ["check_runs_http", "check_runs_browser"]
WEBHOOK_TOLERANCE_S = 300
BILLED_STATUSES = {"active", "past_due", "trialing"}

_ssm = None
_settings = {}


class BillingError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


SETTINGS_TTL_S = 300


def settings():
    """{secret_key, webhook_secret, prices, events, portal_configuration}, re-read every
    SETTINGS_TTL_S (so new keys or prices apply within that); {} when not set up."""
    global _ssm
    if "loaded" not in _settings or time.time() - _settings["loaded"] > SETTINGS_TTL_S:
        _ssm = _ssm or boto3.client("ssm")
        out = {}
        try:
            for name in ("secret_key", "webhook_secret"):
                out[name] = _ssm.get_parameter(Name=f"{PARAMS}/{name}", WithDecryption=True)["Parameter"]["Value"]
            out.update(json.loads(_ssm.get_parameter(Name=f"{PARAMS}/config")["Parameter"]["Value"]))
        except Exception as e:   # noqa: BLE001  not set up (or not readable): billing is off
            print(json.dumps({"billing": "not configured", "error": str(e)[:200]}))
            out = {}
        _settings.clear()
        _settings.update(out, loaded=time.time())
    return _settings if "secret_key" in _settings else {}


def _encode(data, prefix=""):
    """Stripe's form encoding: nested dicts and lists as a[b][0]=..."""
    out = []
    items = data.items() if isinstance(data, dict) else enumerate(data)
    for k, v in items:
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, (dict, list)):
            out += _encode(v, key)
        elif v is not None:
            out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out


def stripe(method, path, data=None, idempotency_key=None):
    """One Stripe API call -> its JSON. Raises BillingError with Stripe's message on failure."""
    s = settings()
    if not s:
        raise BillingError(503, "billing isn't set up yet; contact us to subscribe")
    body = urllib.parse.urlencode(_encode(data or {})).encode() if method != "GET" else None
    url = API + path + (("?" + urllib.parse.urlencode(_encode(data or {}))) if method == "GET" and data else "")
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {s['secret_key']}", "Content-Type": "application/x-www-form-urlencoded",
        "Stripe-Version": "2025-03-31.basil", **({"Idempotency-Key": idempotency_key} if idempotency_key else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise BillingError(502, f"Stripe: {msg or e.code}")


# ------------------------------------------------------------------ checkout and portal

def checkout(tenant, email, record):
    """-> {url}: Stripe Checkout for the subscription, with every metered price (nothing is charged
    until usage is reported). Reuses the tenant's Stripe customer if it has one."""
    if record.get("billing_status") in BILLED_STATUSES and record.get("stripe_subscription_id"):
        raise BillingError(409, "this account already has a subscription: use Manage billing")
    prices = settings().get("prices") if settings() else None
    if not prices:
        raise BillingError(503, "billing isn't set up yet; contact us to subscribe")
    session = stripe("POST", "/checkout/sessions", {
        "mode": "subscription",
        "line_items": [{"price": prices[m]} for m in METERS if m in prices],
        "client_reference_id": tenant,
        "metadata": {"tenant": tenant},
        "subscription_data": {"metadata": {"tenant": tenant}},
        **({"customer": record["stripe_customer_id"], "customer_update": {"name": "auto", "address": "auto"}}
           if record.get("stripe_customer_id") else {"customer_email": email}),
        "billing_address_collection": "auto",
        "tax_id_collection": {"enabled": True},
        "success_url": f"{APP_URL}/#/settings?billing=done",
        "cancel_url": f"{APP_URL}/#/settings?billing=cancelled",
    })
    return {"url": session["url"]}


def portal(record):
    if not record.get("stripe_customer_id"):
        raise BillingError(400, "no billing account yet: add payment details first")
    session = stripe("POST", "/billing_portal/sessions",
                     {"customer": record["stripe_customer_id"], "return_url": f"{APP_URL}/#/settings",
                      "configuration": settings().get("portal_configuration")})
    return {"url": session["url"]}


# ------------------------------------------------------------------ webhook

def verify(raw_body, header, secret, now=None):
    """Stripe-Signature check (HMAC-SHA256 of "<t>.<body>"), within WEBHOOK_TOLERANCE_S. -> the event."""
    parts = {}
    for item in (header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        t = int(parts.get("t", ["0"])[0])
    except ValueError:
        t = 0
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
    if not t or not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise BillingError(400, "bad signature")
    if abs((now or time.time()) - t) > WEBHOOK_TOLERANCE_S:
        raise BillingError(400, "signature too old")
    return json.loads(raw_body)


def webhook(raw_body, header, on_subscribed, set_billing):
    """Handle one Stripe event. on_subscribed(tenant, customer, subscription, email): the first
    subscription (upgrade the account); set_billing(tenant, **fields): later changes."""
    s = settings()
    if not s:
        raise BillingError(503, "billing isn't set up")
    event = verify(raw_body, header, s["webhook_secret"])
    kind, obj = event.get("type"), (event.get("data") or {}).get("object") or {}
    tenant = (obj.get("metadata") or {}).get("tenant") or obj.get("client_reference_id")
    if kind == "checkout.session.completed" and obj.get("mode") == "subscription" and tenant:
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
        on_subscribed(tenant, obj.get("customer"), obj.get("subscription"), email)
    elif kind in ("customer.subscription.updated", "customer.subscription.deleted") and tenant:
        status = "canceled" if kind.endswith("deleted") else obj.get("status")
        set_billing(tenant, billing_status=status, subscription=obj.get("id"))
    print(json.dumps({"stripe_event": kind, "id": event.get("id"), "tenant": tenant}))
    return {"received": True}


# ------------------------------------------------------------------ usage

def day_usage(meter_item, usage_items):
    """One tenant's day: {meter: value}. meter_item: its meter#<tenant>#<day> (received, dropped,
    check runs); usage_items: its obs-usage records of the day (records stored per chunk)."""
    out = {f"ingest_{s}": int(meter_item.get(f"in_{s}", 0)) for s in SIGNALS}
    for s in SIGNALS:
        out[f"stored_{s}"] = sum(int(u.get("records", 0)) for u in usage_items if u.get("signal") == s)
    out["check_runs_http"] = int(meter_item.get("checks_http", 0))
    out["check_runs_browser"] = int(meter_item.get("checks_browser", 0))
    return out


def report(tenant, customer, day, usage):
    """Send a day's usage to Stripe's meters, one event per non-zero meter, at the day's last second.
    Identifiers make a resend of the same event harmless."""
    events = settings().get("events") or {}
    ts = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86_399
    sent = {}
    for meter, value in usage.items():
        if value <= 0 or meter not in events:
            continue
        stripe("POST", "/billing/meter_events", {
            "event_name": events[meter], "timestamp": ts, "identifier": f"{tenant}:{day}:{meter}",
            "payload": {"stripe_customer_id": customer, "value": value}})
        sent[meter] = value
    return sent
