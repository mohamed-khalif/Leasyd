#!/usr/bin/env python3
"""Sets up Stripe for Leasyd's usage-based billing, and stores the settings in SSM (/obs/stripe/*),
where obs-account-api and obs-tenant-admin read them (services/tenants/billing.py).

  STRIPE_SECRET_KEY=sk_test_... STRIPE_APP_KEY=rk_test_... AWS_DEFAULT_REGION=us-east-1 python3 infra/stripe-setup.py

STRIPE_SECRET_KEY sets Stripe up (used here only, never stored). STRIPE_APP_KEY is the key Leasyd
runs with, stored in SSM: a restricted key (rk_...) with only these permissions, Write on each:
Checkout Sessions, Customer portal, Customers, Subscriptions, Billing Meter Events (others None).
Without it, the secret key is stored instead (works, but has every permission).

Run it with a test-mode key first (sk_test_...); later again with the live key (sk_live_...), which
replaces the settings. Safe to repeat: what exists is reused (meters by event name, prices by lookup
key, the product by id, the webhook endpoint by URL).

It creates, in that Stripe account:
  - a product "Leasyd" and, per meter below, a Stripe billing meter (sum of "value", per customer)
    and a monthly metered price on it, at the list prices (services/web/src/pricing.ts)
  - a customer portal configuration (card, invoices, billing details, cancel at period end)
  - a webhook endpoint https://<API_DOMAIN>/v1/stripe/webhook for the subscription events
The keys never go in the repository: only into SSM SecureStrings.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import boto3

API = "https://api.stripe.com/v1"
VERSION = "2025-03-31.basil"
KEY = os.environ.get("STRIPE_SECRET_KEY", "")
APP_KEY = os.environ.get("STRIPE_APP_KEY", "") or KEY
API_DOMAIN = os.environ.get("API_DOMAIN", "ingest.leasyd.com")
PARAMS = "/obs/stripe"
PRODUCT = "leasyd_usage"
EVENTS = ["checkout.session.completed", "customer.subscription.updated", "customer.subscription.deleted"]

# meter -> (name on the invoice, US cents per unit). Per record: $0.05 per million = 0.000005 cents.
METERS = {
    "ingest_traces": ("Spans ingested", "0.000005"),
    "ingest_logs": ("Log records ingested", "0.000005"),
    "ingest_metrics": ("Metric data points ingested", "0.0000015"),
    "stored_traces": ("Spans stored (30 days)", "0.000045"),
    "stored_logs": ("Log records stored (30 days)", "0.000045"),
    "stored_metrics": ("Metric data points stored (30 days)", "0.000015"),
    "check_runs_http": ("HTTP check runs", "0.018"),
    "check_runs_browser": ("Browser check runs", "0.3"),
}


def encode(data, prefix=""):
    out = []
    for k, v in (data.items() if isinstance(data, dict) else enumerate(data)):
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, (dict, list)):
            out += encode(v, key)
        elif v is not None:
            out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out


def stripe(method, path, data=None):
    q = urllib.parse.urlencode(encode(data or {}))
    url = API + path + (f"?{q}" if method == "GET" and q else "")
    req = urllib.request.Request(url, data=q.encode() if method != "GET" else None, method=method, headers={
        "Authorization": f"Bearer {KEY}", "Stripe-Version": VERSION,
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        err = json.loads(e.read() or b"{}").get("error", {})
        raise SystemExit(f"Stripe {method} {path}: {err.get('message') or e.code}")


def listed(path, **params):
    items, after = [], None
    while True:
        page = stripe("GET", path, {"limit": 100, **params, **({"starting_after": after} if after else {})})
        items += page["data"]
        if not page.get("has_more"):
            return items
        after = page["data"][-1]["id"]


def main():
    if not KEY.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        sys.exit("set STRIPE_SECRET_KEY to your Stripe secret key (sk_test_... first)")
    mode = "LIVE" if "_live_" in KEY else "test"
    if not APP_KEY.startswith(("rk_", "sk_")) or ("_live_" in APP_KEY) != ("_live_" in KEY):
        sys.exit(f"STRIPE_APP_KEY must be a {mode}-mode restricted key (rk_{'live' if mode == 'LIVE' else 'test'}_...)")
    if APP_KEY == KEY:
        print("  note: storing the secret key for Leasyd to run with; a restricted key (STRIPE_APP_KEY) is safer")
    print(f"Stripe account in {mode} mode")

    try:
        stripe("GET", f"/products/{PRODUCT}")
    except SystemExit:
        stripe("POST", "/products", {"id": PRODUCT, "name": "Leasyd",
                                     "description": "Observability: logs, traces, metrics and synthetic checks"})
    meters = {m["event_name"]: m for m in listed("/billing/meters", status="active")}
    prices = {p["lookup_key"]: p for p in listed("/prices", active="true", product=PRODUCT) if p.get("lookup_key")}
    config = {"prices": {}, "events": {}}
    for meter, (name, cents) in METERS.items():
        event = f"leasyd_{meter}"
        m = meters.get(event) or stripe("POST", "/billing/meters", {
            "display_name": name, "event_name": event, "default_aggregation": {"formula": "sum"},
            "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
            "value_settings": {"event_payload_key": "value"}})
        lookup = f"leasyd_{meter}"
        p = prices.get(lookup)
        if p and (float(p.get("unit_amount_decimal") or 0) != float(cents) or (p.get("recurring") or {}).get("meter") != m["id"]):
            p = None    # a changed price: a new one takes the lookup key (subscriptions keep the old one)
        p = p or stripe("POST", "/prices", {
            "product": PRODUCT, "currency": "usd", "nickname": name, "lookup_key": lookup, "transfer_lookup_key": True,
            "unit_amount_decimal": cents, "billing_scheme": "per_unit",
            "recurring": {"interval": "month", "usage_type": "metered", "meter": m["id"]}})
        config["prices"][meter], config["events"][meter] = p["id"], event
        print(f"  {name:38} {meter:20} meter {m['id']}  price {p['id']} ({cents} cents each)")

    ssm = boto3.client("ssm")
    try:
        before = json.loads(ssm.get_parameter(Name=f"{PARAMS}/config")["Parameter"]["Value"])
    except Exception:   # noqa: BLE001  the first run
        before = {}
    known = {c["id"] for c in listed("/billing_portal/configurations", active="true")}
    pc = before.get("portal_configuration")
    portal = stripe("POST", "/billing_portal/configurations" + (f"/{pc}" if pc in known else ""), {
        "business_profile": {"headline": "Leasyd billing"},
        "features": {"invoice_history": {"enabled": True}, "payment_method_update": {"enabled": True},
                     "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "tax_id", "name"]},
                     "subscription_cancel": {"enabled": True, "mode": "at_period_end"}}})
    config["portal_configuration"] = portal["id"]
    print(f"  customer portal configuration {portal['id']}")

    url = f"https://{API_DOMAIN}/v1/stripe/webhook"
    hooks = [h for h in listed("/webhook_endpoints") if h["url"] == url]
    secret = None
    if hooks:
        try:
            old = ssm.get_parameter(Name=f"{PARAMS}/webhook_secret", WithDecryption=True)["Parameter"]["Value"]
            same_mode = ssm.get_parameter(Name=f"{PARAMS}/endpoint")["Parameter"]["Value"] == hooks[0]["id"]
        except Exception:   # noqa: BLE001  not stored yet, or for another endpoint
            old, same_mode = None, False
        if old and same_mode:
            stripe("POST", f"/webhook_endpoints/{hooks[0]['id']}", {"enabled_events": EVENTS})
            secret, endpoint = old, hooks[0]["id"]
        else:   # its signing secret is shown only when it's created: make it again
            for h in hooks:
                stripe("DELETE", f"/webhook_endpoints/{h['id']}")
    if not secret:
        h = stripe("POST", "/webhook_endpoints", {"url": url, "enabled_events": EVENTS, "api_version": VERSION,
                                                  "description": "Leasyd subscriptions (obs-account-api)"})
        secret, endpoint = h["secret"], h["id"]
    print(f"  webhook {endpoint} -> {url}")

    for name, value, kind in (("secret_key", APP_KEY, "SecureString"), ("webhook_secret", secret, "SecureString"),
                              ("endpoint", endpoint, "String"), ("config", json.dumps(config), "String")):
        ssm.put_parameter(Name=f"{PARAMS}/{name}", Value=value, Type=kind, Overwrite=True)
    print(f"Stored in SSM {PARAMS}/ (secret_key, webhook_secret, endpoint, config).")
    print("Leasyd's functions pick them up within 5 minutes.")


if __name__ == "__main__":
    main()
