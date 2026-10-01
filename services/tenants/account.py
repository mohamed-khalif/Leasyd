"""obs-account-api: self-service sign-up, and a tenant's own account in the app.

  POST /v1/signup                     public: {email, company} -> 202; the account is made in
                                      the background and its owner emailed a temporary password
  GET  /v1/app/account                the tenant: plan, daily cap and today's data, its users
                                      and API keys (never the keys themselves), and who you are
  POST   /v1/app/account/users        owners: invite {email, role?}
  DELETE /v1/app/account/users/{email}   owners: remove someone (not yourself)
  POST   /v1/app/account/keys         owners: a new API key {scope: ingest | read}, shown once
  DELETE /v1/app/account/keys/{id}    owners: revoke a key

The tenant comes only from the signed-in user's token (custom:tenant). Changes are made by
obs-tenant-admin (invoked here), which owns keys, streams and logins; this function only checks
who may ask.

Sign-up abuse: the answer never says whether an email already has an account (that address gets
an email instead), an address is set up at most once an hour, and sign-ups are capped per source
address and per day (SIGNUPS_PER_DAY) across the platform. A hidden form field ("website")
that people leave empty catches simple bots.
"""

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

import emails

TABLE = os.environ["TENANTS_TABLE"]
ADMIN_FUNCTION = os.environ.get("ADMIN_FUNCTION", "obs-tenant-admin")
SIGNUPS_PER_DAY = int(os.environ.get("SIGNUPS_PER_DAY", "100"))
SIGNUPS_PER_ADDRESS = int(os.environ.get("SIGNUPS_PER_ADDRESS", "5"))   # per source address a day
MAX_KEYS, MAX_USERS = 20, 50
RESERVED = {"admin", "api", "app", "www", "leasyd", "canary", "ingest", "status", "support", "test"}
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")

ddb = boto3.resource("dynamodb")
table = ddb.Table(TABLE)
lam = boto3.client("lambda")


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def handler(event, context):
    try:
        resource = event.get("resource") or ""
        method = event.get("httpMethod", "GET")
        body = _body(event)
        if resource == "/v1/signup":
            if method != "POST":
                raise Refused(405, "method not allowed")
            return _http(202, signup(body, _source_address(event)))
        claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
        tenant, email = claims.get("custom:tenant"), (claims.get("email") or "").lower()
        if not tenant or not email:
            raise Refused(401, "sign in first")
        parts = [unquote(p) for p in ((event.get("pathParameters") or {}).get("proxy") or "").split("/") if p]
        return _http(*account(tenant, email, method, parts, body))
    except Refused as e:
        return _http(e.status, {"error": str(e)})


# ------------------------------------------------------------------ sign-up

def signup(body, address):
    if body.get("website"):                     # the hidden field: a bot filled it in
        return _signup_answer()
    email = _email(body.get("email"))
    company = " ".join(str(body.get("company") or "").split())
    if not 2 <= len(company) <= 80:
        raise Refused(400, "company: 2-80 characters")
    day = _now().strftime("%Y-%m-%d")
    _count(f"rate#signup#{day}", SIGNUPS_PER_DAY)
    _count(f"rate#signup#{address}#{day}", SIGNUPS_PER_ADDRESS)
    # At most once an hour per address, whatever happens next (no repeated emails to anyone).
    try:
        table.put_item(Item={"pk": f"signup#{email}", "at": _iso(_now()), "company": company},
                       ConditionExpression="attribute_not_exists(pk) OR #at < :hour_ago",
                       ExpressionAttributeNames={"#at": "at"},
                       ExpressionAttributeValues={":hour_ago": _iso(_now() - timedelta(hours=1))})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _signup_answer()
        raise
    user = table.get_item(Key={"pk": f"user#{email}"}, ConsistentRead=True).get("Item")
    if user and user.get("status") == "active":
        if emails.enabled():
            emails.already_registered(email)
        return _signup_answer()
    tenant = _reserve_tenant(company)
    table.update_item(Key={"pk": f"signup#{email}"}, UpdateExpression="SET tenant = :t",
                      ExpressionAttributeValues={":t": tenant})
    lam.invoke(FunctionName=ADMIN_FUNCTION, InvocationType="Event",
               Payload=json.dumps({"action": "signup", "tenant": tenant, "email": email, "company": company}).encode())
    print(json.dumps({"signup": tenant, "address": address}))
    return _signup_answer()


def _signup_answer():
    return {"status": "accepted",
            "message": "Check your inbox: we're setting up your account and will email you a temporary "
                       "password within a couple of minutes."}


def _reserve_tenant(company):
    """A tenant name from the company's ("Acme Inc." -> acme-inc), made unique, reserved as "creating"."""
    base = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")[:30].strip("-")
    if len(base) < 3 or base in RESERVED or base.startswith(("t6-", "seed-", "leasyd")):
        base = (base + "-team" if len(base) >= 1 else "team")[:30]
    for attempt in range(5):
        name = base if attempt == 0 else f"{base}-{secrets.token_hex(2)}"
        try:
            table.put_item(Item={"pk": f"tenant#{name}", "tenant": name, "status": "creating", "plan": "free",
                                 "company": company, "created_at": _iso(_now())},
                           ConditionExpression="attribute_not_exists(pk)")
            return name
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise Refused(503, "couldn't choose an account name; try again")


def _count(pk, limit):
    try:
        table.update_item(Key={"pk": pk}, UpdateExpression="ADD n :one",
                          ConditionExpression="attribute_not_exists(n) OR n < :max",
                          ExpressionAttributeValues={":one": 1, ":max": limit})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Refused(429, "too many sign-ups right now; please try again later")
        raise


def _source_address(event):
    """The caller's address. Through the app (CloudFront), API Gateway's X-Forwarded-For ends
    "<viewer>, <CloudFront edge>"; called directly, the last entry is the caller."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    chain = [a.strip() for a in (headers.get("x-forwarded-for") or "").split(",") if a.strip()]
    if "cloudfront" in (headers.get("via") or "").lower() and len(chain) >= 2:
        return chain[-2]
    return chain[-1] if chain else ((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp", "unknown")


# ------------------------------------------------------------------ the tenant's account

def account(tenant, email, method, parts, body):
    me = table.get_item(Key={"pk": f"user#{email}"}).get("Item")
    if not me or me.get("tenant") != tenant or me.get("status") != "active":
        raise Refused(403, "you are not a user of this account")
    role = me.get("role", "owner")
    if not parts:
        if method != "GET":
            raise Refused(405, "method not allowed")
        st, us = _admin("status", tenant), _admin("users", tenant)
        keys = [{"key_id": k["api_key_id"], "scope": k.get("scope", "ingest"), "status": k["status"],
                 "created_at": k.get("created_at"), "expires_at": k.get("expires_at")}
                for k in st["keys"] if k["status"] in ("active", "expiring")]
        return 200, {"tenant": tenant, "company": st.get("company") or tenant, "plan": st.get("plan"),
                     "daily_cap_bytes": st.get("daily_cap_bytes"), "today": st.get("today"),
                     "created_at": st.get("created_at"), "you": {"email": email, "role": role},
                     "users": [u for u in us["users"] if u["status"] == "active"], "keys": keys,
                     "limits": {"keys": MAX_KEYS, "users": MAX_USERS}}
    if role != "owner":
        raise Refused(403, "only the account's owners can change its users and keys")
    what, ident = parts[0], (parts[1] if len(parts) > 1 else None)
    if what == "users" and method == "POST" and ident is None:
        users = [u for u in _admin("users", tenant)["users"] if u["status"] == "active"]
        if len(users) >= MAX_USERS:
            raise Refused(400, f"at most {MAX_USERS} users")
        r = body.get("role", "member")
        out = _admin("invite-user", tenant, email=_email(body.get("email")), role=r, invited_by=email)
        return 201, out
    if what == "users" and method == "DELETE" and ident:
        if ident.lower() == email:
            raise Refused(400, "you can't remove yourself")
        return 200, _admin("remove-user", tenant, email=_email(ident))
    if what == "keys" and method == "POST" and ident is None:
        live = [k for k in _admin("status", tenant)["keys"] if k["status"] in ("active", "expiring")]
        if len(live) >= MAX_KEYS:
            raise Refused(400, f"at most {MAX_KEYS} keys; revoke one first")
        scope = body.get("scope", "ingest")
        out = _admin("add-key", tenant, scope=scope)
        print(json.dumps({"key_created": out["key_id"], "tenant": tenant, "by": email, "scope": scope}))
        return 201, {"key_id": out["key_id"], "scope": out["scope"], "api_key": out["api_key"]}
    if what == "keys" and method == "DELETE" and ident and _KEY_ID.match(ident):
        out = _admin("revoke", tenant, key_id=ident)
        print(json.dumps({"key_revoked": ident, "tenant": tenant, "by": email}))
        return 200, out
    raise Refused(404, "not found")


def _admin(action, tenant, **kw):
    r = lam.invoke(FunctionName=ADMIN_FUNCTION, Payload=json.dumps({"action": action, "tenant": tenant, **kw}).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    if r.get("FunctionError"):
        raise RuntimeError(f"obs-tenant-admin {action} failed: {out}")
    if "error" in out:
        raise Refused(400, out["error"])
    return out


# ------------------------------------------------------------------ helpers

def _email(v):
    e = str(v or "").strip().lower()
    if not _EMAIL.match(e):
        raise Refused(400, "email: an email address")
    return e


def _body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    try:
        b = json.loads(raw) if raw else {}
    except ValueError:
        raise Refused(400, "body: a JSON object")
    if not isinstance(b, dict):
        raise Refused(400, "body: a JSON object")
    return b


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _http(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}
