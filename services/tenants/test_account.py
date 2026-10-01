"""obs-account-api against moto: self-service sign-up and a tenant's own account (users, keys).
obs-tenant-admin runs in-process (its real handler) instead of being invoked."""

import json

import boto3
import pytest

from test_admin import CTX, adm, call, login, mail, message  # noqa: F401  (fixtures)


@pytest.fixture
def acc(adm, monkeypatch):  # noqa: F811
    import importlib
    import account
    importlib.reload(account)
    started = []

    class Lam:   # sync calls run obs-tenant-admin here; async ones (sign-up) are kept to run by hand
        def invoke(self, FunctionName, Payload, InvocationType="RequestResponse"):
            event = json.loads(Payload)
            if InvocationType == "Event":
                started.append(event)
                return {}
            out = json.dumps(adm.handler(event, CTX)).encode()
            return {"Payload": type("P", (), {"read": lambda self: out})()}
    monkeypatch.setattr(account, "lam", Lam())
    account.started = started
    return account


def signup(acc, body, ip="203.0.113.7", via_cloudfront=False):
    headers = {"X-Forwarded-For": f"{ip}, 198.51.100.1" if via_cloudfront else ip}
    if via_cloudfront:
        headers["Via"] = "2.0 abc.cloudfront.net (CloudFront)"
    out = acc.handler({"resource": "/v1/signup", "httpMethod": "POST", "headers": headers, "body": json.dumps(body)}, None)
    return out["statusCode"], json.loads(out["body"])


def app(acc, who, tenant, method="GET", path="", body=None):
    out = acc.handler({"resource": "/v1/app/account" + ("/{proxy+}" if path else ""), "httpMethod": method,
                       "pathParameters": {"proxy": path} if path else None, "body": json.dumps(body) if body else None,
                       "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": who}}}}, None)
    return out["statusCode"], json.loads(out["body"])


def test_signup_to_signed_in_owner(acc, adm, mail):  # noqa: F811
    status, out = signup(acc, {"email": "Ana@Acme.com", "company": "Acme Inc."})
    assert status == 202 and "Check your inbox" in out["message"]
    (job,) = acc.started
    assert job == {"action": "signup", "tenant": "acme-inc", "email": "ana@acme.com", "company": "Acme Inc."}
    adm.handler(job, CTX)                                    # obs-tenant-admin, in the background
    assert login("ana@acme.com")["custom:tenant"] == "acme-inc"
    assert [message(m)["subject"] for m in mail] == ["Your Leasyd account is ready"]
    # Her account: free plan, 1 GB a day, no keys yet; she's the owner.
    status, me = app(acc, "ana@acme.com", "acme-inc")
    assert status == 200 and me["company"] == "Acme Inc." and me["plan"] == "free"
    assert me["daily_cap_bytes"] == 10**9 and me["keys"] == [] and me["you"] == {"email": "ana@acme.com", "role": "owner"}
    # A key, shown once; then listed without it.
    status, key = app(acc, "ana@acme.com", "acme-inc", "POST", "keys", {"scope": "ingest"})
    assert status == 201 and key["api_key"].startswith("obs_") and key["scope"] == "ingest"
    listed = app(acc, "ana@acme.com", "acme-inc")[1]["keys"]
    assert [k["key_id"] for k in listed] == [key["key_id"]] and "api_key" not in listed[0]
    # A teammate, a member: sees the account, can't change it.
    status, inv = app(acc, "ana@acme.com", "acme-inc", "POST", "users", {"email": "bo@acme.com"})
    assert status == 201 and inv["role"] == "member"
    assert "ana@acme.com invited you" in message(mail[-1])["text"]
    assert app(acc, "bo@acme.com", "acme-inc")[1]["you"]["role"] == "member"
    assert app(acc, "bo@acme.com", "acme-inc", "POST", "keys", {"scope": "read"})[0] == 403
    assert app(acc, "bo@acme.com", "acme-inc", "DELETE", "users/ana%40acme.com")[0] == 403
    assert app(acc, "ana@acme.com", "acme-inc", "DELETE", "users/ana%40acme.com") == (400, {"error": "you can't remove yourself"})
    # Revoke, remove.
    assert app(acc, "ana@acme.com", "acme-inc", "DELETE", f"keys/{key['key_id']}")[1]["revoked"] == [key["key_id"]]
    assert app(acc, "ana@acme.com", "acme-inc")[1]["keys"] == []
    assert app(acc, "ana@acme.com", "acme-inc", "DELETE", "users/bo%40acme.com")[1]["status"] == "removed"
    assert login("bo@acme.com") is None


def test_accounts_are_separate(acc, adm):  # noqa: F811
    call(adm, "create", tenant="acme")
    call(adm, "create", tenant="beta")
    call(adm, "invite-user", tenant="acme", email="ana@acme.com", send_email=False)
    beta_key = call(adm, "status", tenant="beta")["keys"][0]["api_key_id"]
    # A token claiming another tenant than the user's: refused.
    assert app(acc, "ana@acme.com", "beta")[0] == 403
    # Ana can't revoke beta's key through her own account.
    assert app(acc, "ana@acme.com", "acme", "DELETE", f"keys/{beta_key}")[0] == 400
    assert call(adm, "status", tenant="beta")["keys"][0]["status"] == "active"
    assert app(acc, "nobody@x.com", "acme")[0] == 403
    assert acc.handler({"resource": "/v1/app/account", "httpMethod": "GET", "requestContext": {}}, None)["statusCode"] == 401


def test_signup_never_tells_who_has_an_account_and_is_limited(acc, adm, mail, monkeypatch):  # noqa: F811
    call(adm, "create", tenant="acme")
    call(adm, "invite-user", tenant="acme", email="ana@acme.com", send_email=False)
    status, out = signup(acc, {"email": "ana@acme.com", "company": "Another"})
    assert status == 202 and acc.started == []               # same answer; no account made...
    assert [message(m)["subject"] for m in mail] == ["You already have a Leasyd account"]   # ...she's told by email
    assert signup(acc, {"email": "ana@acme.com", "company": "Another"})[0] == 202
    assert len(mail) == 1                                    # at most once an hour per address
    # A taken name gets a suffix; bots filling the hidden field get the same answer, nothing else.
    signup(acc, {"email": "x@acme.com", "company": "Acme"})
    assert acc.started[-1]["tenant"].startswith("acme-") and len(acc.started[-1]["tenant"]) == 9
    assert signup(acc, {"email": "bot@spam.com", "company": "Spam", "website": "http://spam"})[0] == 202
    assert len(acc.started) == 1
    # Bad input.
    assert signup(acc, {"email": "nope", "company": "X Co"}) == (400, {"error": "email: an email address"})
    assert signup(acc, {"email": "a@b.co", "company": "X"})[0] == 400
    # Per source address (the viewer's, through CloudFront), and per day.
    monkeypatch.setattr(acc, "SIGNUPS_PER_ADDRESS", 2)
    assert signup(acc, {"email": "p1@q.com", "company": "Pq"}, ip="192.0.2.9", via_cloudfront=True)[0] == 202
    assert signup(acc, {"email": "p2@q.com", "company": "Pq"}, ip="192.0.2.9", via_cloudfront=True)[0] == 202
    assert signup(acc, {"email": "p3@q.com", "company": "Pq"}, ip="192.0.2.9", via_cloudfront=True)[0] == 429
    assert signup(acc, {"email": "p3@q.com", "company": "Pq"}, ip="192.0.2.10", via_cloudfront=True)[0] == 202
    monkeypatch.setattr(acc, "SIGNUPS_PER_DAY", 1)
    assert signup(acc, {"email": "p4@q.com", "company": "Pq"}, ip="192.0.2.11")[0] == 429


def test_reserved_and_short_names(acc):
    assert acc._reserve_tenant("Admin") == "admin-team"
    assert acc._reserve_tenant("X") == "x-team"
    assert acc._reserve_tenant("Ünïcode & Co!!") == "n-code-co"
    item = boto3.resource("dynamodb").Table("obs-tenants").get_item(Key={"pk": "tenant#n-code-co"})["Item"]
    assert item["status"] == "creating" and item["company"] == "Ünïcode & Co!!"
