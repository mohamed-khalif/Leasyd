"""obs-investigator: where investigations start, and the app's view of them.

  {"alert": <payload>, "tenant": T}                 invoked (async) by obs-alerts when a rule fires
  POST /v1/investigations/webhook/{token}           public: CloudWatch (SNS), PagerDuty or any tool; the
                                                    token alone decides the tenant (unknown: 404)
  GET  /v1/app/investigations                       signed in: the tenant's investigations, newest first
  GET  /v1/app/investigations/{id}                  one, in full (trigger, events, result)
  POST /v1/app/investigations/webhook-token         owners: a new webhook URL (shown once; the old one stops)
  GET|PUT /v1/app/investigations/settings           where investigations go: {channels, services, fallback}
                                                    (PUT: owners)
  POST /v1/app/investigations/{id}/feedback         signed in: {"verdict": right|wrong, "note"}
  GET|POST /v1/investigations/feedback/{token}      public: a delivered Right/Wrong link. GET shows a page
                                                    and records nothing; its button POSTs the answer, once
  POST /v1/deploys                                  CI records a deploy, with an ingest API key (the key's
                                                    tenant, set by the API's key authorizer)
  GET  /v1/app/deploys?service=&days=               signed in: the tenant's recorded deploys, newest first

Every route takes the tenant from the alert's own record, the webhook token, the API key or the
sign-in token, never from the request body.
"""

import base64
import hashlib
import json
import re
import secrets
import urllib.request
from urllib.parse import parse_qs
from datetime import datetime, timedelta, timezone

from domain import deploys, triggers
from domain.delivery import settings_from
from domain.workflow import give_feedback, iso, open_investigation
from handlers import deps, feedback_page

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
        if resource == "/v1/investigations/feedback/{token}":
            return feedback_link(event)
        if resource == "/v1/deploys":
            tenant = ((event.get("requestContext") or {}).get("authorizer") or {}).get("tenant")
            if not tenant:
                raise Refused(401, "send x-api-key")
            return _http(*record_deploy(tenant, _body(event)))
        claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
        tenant, user = claims.get("custom:tenant"), (claims.get("email") or "").lower()
        if not tenant or not user:
            raise Refused(401, "sign in first")
        if resource == "/v1/app/deploys":
            return _http(*list_deploys(tenant, event.get("queryStringParameters") or {}))
        method = event.get("httpMethod", "GET")
        return _http(*app(tenant, user, method, ((event.get("pathParameters") or {}).get("proxy") or "").strip("/"),
                          _body(event) if method in ("POST", "PUT") else None))
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


def record_deploy(tenant, body):
    now = deps.clock().now()
    try:
        d = deploys.parse(body, datetime.fromtimestamp(now, timezone.utc))
    except ValueError as e:
        raise Refused(400, str(e))
    deps.store().put_deploy(tenant, d, now)
    print(json.dumps({"deploy": d.id, "tenant": tenant, "service": d.service, "version": d.version, "status": d.status}))
    return 201, {"deploy": d.id, "deployed_at": d.deployed_at}


def list_deploys(tenant, q):
    try:
        days = min(max(int(q.get("days") or 7), 1), deploys.KEEP_DAYS)
    except ValueError:
        raise Refused(400, "days: a number")
    end = datetime.fromtimestamp(deps.clock().now(), timezone.utc)
    found = deps.store().deploys(tenant, iso((end - timedelta(days=days)).timestamp()), iso(end.timestamp() + 60), q.get("service") or None)
    return 200, {"deploys": found[::-1]}


def app(tenant, user, method, path, body=None):
    store = deps.store()
    if method == "GET" and not path:
        return 200, {"investigations": store.list(tenant)}
    if method == "GET" and _ID.match(path):
        inv = store.get(tenant, path)
        if not inv:
            raise Refused(404, "no such investigation")
        return 200, inv.to_dict()
    if path == "settings" and method == "GET":
        return 200, {"settings": deps.channels().settings(tenant)}
    if path == "settings" and method == "PUT":
        _owner(store, tenant, user, "change where investigations go")
        try:
            settings = settings_from(body or {}, lambda cid: deps.channels().exists(tenant, cid))
        except ValueError as e:
            raise Refused(400, str(e))
        deps.channels().save_settings(tenant, settings, user, iso(deps.clock().now()))
        return 200, {"settings": settings}
    parts = path.split("/")
    if method == "POST" and len(parts) == 2 and _ID.match(parts[0]) and parts[1] == "feedback":
        try:
            inv, _ = give_feedback(store, deps.clock(), tenant, parts[0], (body or {}).get("verdict"), (body or {}).get("note"),
                                   by=user, replace=True)
        except ValueError as e:
            raise Refused(400, str(e))
        if not inv:
            raise Refused(404, "no such investigation")
        return 200, {"feedback": inv.feedback}
    if method == "POST" and path == "webhook-token":
        _owner(store, tenant, user, "make a webhook URL")
        token = store.new_webhook_token(tenant, user, iso(deps.clock().now()))
        return 201, {"url": f"{deps.API_URL}/v1/investigations/webhook/{token}",
                     "note": "Shown once. Any previous webhook URL has stopped working."}
    raise Refused(404, "not found")


def _owner(store, tenant, user, what):
    me = store.table.get_item(Key={"pk": f"user#{user}"}).get("Item") or {}
    if me.get("tenant") != tenant or me.get("status") != "active" or me.get("role", "owner") != "owner":
        raise Refused(403, f"only the account's owners can {what}")


def feedback_link(event):
    """A Right/Wrong link: GET asks (records nothing), POST answers (uses the link up)."""
    token = (event.get("pathParameters") or {}).get("token") or ""
    if not _TOKEN.match(token):
        return feedback_page.gone()
    store, now = deps.store(), deps.clock().now()
    if event.get("httpMethod") != "POST":
        held = store.feedback_token(token, now)
        inv = held and store.get(held["tenant"], held["inv_id"])
        if not inv or not inv.result:
            return feedback_page.gone()
        if inv.feedback:
            return feedback_page.answered(inv)
        return feedback_page.ask(inv, ((event.get("queryStringParameters") or {}).get("v") or ""))
    form = parse_qs(_raw(event), max_num_fields=5)
    verdict, note = (form.get("verdict") or [""])[0], (form.get("note") or [""])[0]
    if verdict not in ("right", "wrong"):
        return feedback_page.bad("Choose Right or Wrong, then press the button.")
    held = store.feedback_token(token, now, use=True)
    if not held:
        return feedback_page.gone()
    inv, recorded = give_feedback(store, deps.clock(), held["tenant"], held["inv_id"], verdict, note, by="link")
    if not inv:
        return feedback_page.gone()
    print(json.dumps({"investigation_feedback": inv.id, "tenant": inv.tenant, "verdict": verdict, "recorded": recorded}))
    return feedback_page.thanks(verdict) if recorded else feedback_page.answered(inv)


def _raw(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):     # the API passes every body through as binary
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw) > MAX_BODY:
        raise Refused(413, f"body: at most {MAX_BODY // 1024} KB")
    return raw


def _body(event):
    raw = _raw(event)
    try:
        return json.loads(raw or "{}")
    except ValueError:
        raise Refused(400, "body: JSON")


def _http(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}
