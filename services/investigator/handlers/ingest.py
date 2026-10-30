"""obs-investigator: where investigations start, and the app's view of them.

  {"alert": <payload>, "tenant": T}                 invoked (async) by obs-alerts when a rule fires
  POST /v1/investigations/webhook/{token}           public: CloudWatch (SNS), PagerDuty or any tool; the
                                                    token alone decides the tenant (unknown: 404)
  GET  /v1/app/investigations                       signed in: the tenant's investigations, newest first
  GET  /v1/app/investigations/{id}                  one, in full (trigger, events, result)
  POST /v1/app/investigations/webhook-token         owners: a new webhook URL (shown once; the old one stops)

Every route takes the tenant from the alert's own record, the webhook token or the sign-in token,
never from the request body.
"""

import base64
import hashlib
import json
import re
import secrets
import urllib.request

from domain import triggers
from domain.workflow import iso, open_investigation
from handlers import deps

MAX_BODY = 256 * 1024
_ID = re.compile(r"^[0-9a-f]{16}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def handler(event, context=None):
    if "alert" in event:
        return from_alert(event["tenant"], event["alert"])
    try:
        resource = event.get("resource") or ""
        if resource == "/v1/investigations/webhook/{token}":
            return _http(*webhook((event.get("pathParameters") or {}).get("token") or "", _body(event)))
        claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
        tenant, user = claims.get("custom:tenant"), (claims.get("email") or "").lower()
        if not tenant or not user:
            raise Refused(401, "sign in first")
        return _http(*app(tenant, user, event.get("httpMethod", "GET"),
                          ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")))
    except Refused as e:
        return _http(e.status, {"error": str(e)})


def from_alert(tenant, payload):
    kind, x = triggers.from_alert(payload)
    if kind != "trigger":
        return {"ignored": x}
    inv, joined = open_investigation(deps.store(), deps.queue(), deps.clock(), tenant, x, secrets.token_hex(8), deps.CAP)
    print(json.dumps({"investigation": inv.id, "tenant": tenant, "source": "alert", "joined": joined, "status": inv.status}))
    return {"investigation": inv.id, "joined": joined, "status": inv.status}


def webhook(token, body):
    tenant = deps.store().tenant_of_webhook(hashlib.sha256(token.encode()).hexdigest()) if _TOKEN.match(token) else None
    if not tenant:
        raise Refused(404, "not found")
    try:
        kind, x = triggers.from_webhook(body)
    except ValueError as e:
        raise Refused(400, str(e))
    if kind == "confirm":            # SNS: confirming the subscription is a GET of its (checked) AWS URL
        with urllib.request.urlopen(x, timeout=10) as r:   # noqa: S310  only https://sns.<region>.amazonaws.com
            r.read()
        print(json.dumps({"sns_subscription_confirmed": tenant}))
        return 200, {"confirmed": True}
    if kind == "ignore":
        return 202, {"ignored": x}
    inv, joined = open_investigation(deps.store(), deps.queue(), deps.clock(), tenant, x, secrets.token_hex(8), deps.CAP)
    print(json.dumps({"investigation": inv.id, "tenant": tenant, "source": x.source, "joined": joined, "status": inv.status}))
    return 202, {"investigation": inv.id, "joined": joined, "status": inv.status}


def app(tenant, user, method, path):
    store = deps.store()
    if method == "GET" and not path:
        return 200, {"investigations": store.list(tenant)}
    if method == "GET" and _ID.match(path):
        inv = store.get(tenant, path)
        if not inv:
            raise Refused(404, "no such investigation")
        return 200, inv.to_dict()
    if method == "POST" and path == "webhook-token":
        me = store.table.get_item(Key={"pk": f"user#{user}"}).get("Item") or {}
        if me.get("tenant") != tenant or me.get("status") != "active" or me.get("role", "owner") != "owner":
            raise Refused(403, "only the account's owners can make a webhook URL")
        token = store.new_webhook_token(tenant, user, iso(deps.clock().now()))
        return 201, {"url": f"{deps.API_URL}/v1/investigations/webhook/{token}",
                     "note": "Shown once. Any previous webhook URL has stopped working."}
    raise Refused(404, "not found")


def _body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):     # the API passes every body through as binary
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw) > MAX_BODY:
        raise Refused(413, f"body: at most {MAX_BODY // 1024} KB")
    try:
        return json.loads(raw or "{}")
    except ValueError:
        raise Refused(400, "body: JSON")


def _http(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}
