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
    assert me["trial_ends_at"] > adm._iso(adm._now() + __import__("datetime").timedelta(days=6, hours=23))   # 7-day trial
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


def test_trial_upgrade_and_extension(acc, adm, mail, monkeypatch):  # noqa: F811
    # As API Gateway: a key can't be in two usage plans of one stage at once.
    real = adm.apigw.create_usage_plan_key
    def one_plan(usagePlanId, keyId, keyType):
        for p in adm.apigw.get_usage_plans()["items"]:
            if keyId in [k["id"] for k in adm.apigw.get_usage_plan_keys(usagePlanId=p["id"])["items"]]:
                raise RuntimeError(f"ConflictException: {keyId} is already in usage plan {p['id']}")
        return real(usagePlanId=usagePlanId, keyId=keyId, keyType=keyType)
    monkeypatch.setattr(adm.apigw, "create_usage_plan_key", one_plan)
    signup(acc, {"email": "ana@acme.com", "company": "Acme"})
    adm.handler(acc.started[-1], CTX)
    key = app(acc, "ana@acme.com", "acme", "POST", "keys", {"scope": "ingest"})[1]
    assert call(adm, "status", tenant="acme")["keys"][0]["plan"] == "free"
    # Extend: a new end date; then upgrade: standard plan, no trial end or cap, key moved.
    assert call(adm, "extend-trial", tenant="acme", days=3)["trial_ends_at"] > adm._iso(adm._now())
    assert "days" in call(adm, "extend-trial", tenant="acme", days=500)["error"]
    out = call(adm, "upgrade", tenant="acme")
    assert out == {"tenant": "acme", "plan": "standard", "was": "free", "keys_moved": [key["key_id"]]}
    st = call(adm, "status", tenant="acme")
    assert st["plan"] == "standard" and "trial_ends_at" not in st and "daily_cap_bytes" not in st
    assert st["keys"][0]["plan"] == "standard"
    plans = {p["name"]: p["id"] for p in boto3.client("apigateway").get_usage_plans()["items"]}
    in_plan = lambda name: [k["id"] for k in boto3.client("apigateway").get_usage_plan_keys(usagePlanId=plans[name])["items"]]  # noqa: E731
    assert key["key_id"] in in_plan("obs-standard") and key["key_id"] not in in_plan("obs-free")
    assert app(acc, "ana@acme.com", "acme")[1]["trial_ends_at"] is None


def _owner_and_member(acc, adm):  # noqa: F811
    signup(acc, {"email": "ana@acme.com", "company": "Acme"})
    adm.handler(acc.started.pop(), CTX)
    app(acc, "ana@acme.com", "acme", "POST", "users", {"email": "bo@acme.com"})


def test_drop_rules_saved_by_owners_read_by_everyone(acc, adm, mail):  # noqa: F811
    _owner_and_member(acc, adm)
    assert app(acc, "ana@acme.com", "acme", "GET", "drop-rules")[1]["rules"] == []
    rules = [{"name": "Debug logs", "signal": "logs", "keep_percent": 0,
              "conditions": [{"field": "severity_number", "op": "<", "value": 9}]},
             {"name": "Health checks", "signal": "traces", "keep_percent": 10,
              "conditions": [{"field": "attributes.http.route", "op": "in", "value": ["/health", "/ready"]}]}]
    status, out = app(acc, "ana@acme.com", "acme", "PUT", "drop-rules", {"rules": rules})
    assert status == 200 and [r["name"] for r in out["rules"]] == ["Debug logs", "Health checks"]
    assert all(r["id"] and r["enabled"] for r in out["rules"])
    got = app(acc, "bo@acme.com", "acme", "GET", "drop-rules")[1]
    assert got["rules"] == out["rules"] and got["updated_by"] == "ana@acme.com"
    assert app(acc, "bo@acme.com", "acme", "PUT", "drop-rules", {"rules": []})[0] == 403
    # Stored where ingest reads them.
    item = boto3.resource("dynamodb").Table("obs-tenants").get_item(Key={"pk": "drop#acme"})["Item"]
    assert json.loads(item["rules_json"]) == out["rules"]


@pytest.mark.parametrize("rule,error", [
    ({"signal": "events", "conditions": [{"field": "service", "op": "=", "value": "a"}]}, "signal"),
    ({"signal": "logs", "conditions": []}, "conditions"),
    ({"signal": "logs", "conditions": [{"field": "name", "op": "=", "value": "a"}]}, "unknown field"),
    ({"signal": "logs", "conditions": [{"field": "attributes.a b", "op": "=", "value": "a"}]}, "unknown field"),
    ({"signal": "logs", "conditions": [{"field": "severity_number", "op": "<", "value": "nine"}]}, "number"),
    ({"signal": "logs", "conditions": [{"field": "body", "op": "matches", "value": ".*"}]}, "op"),
    ({"signal": "logs", "keep_percent": 100, "conditions": [{"field": "body", "op": "contains", "value": "x"}]}, "keep_percent"),
    ({"signal": "traces", "conditions": [{"field": "name", "op": "in", "value": "x"}]}, "list"),
])
def test_bad_drop_rules_refused(acc, adm, mail, rule, error):  # noqa: F811
    _owner_and_member(acc, adm)
    status, out = app(acc, "ana@acme.com", "acme", "PUT", "drop-rules", {"rules": [rule]})
    assert status == 400 and error in out["error"]


def test_gpu_prices_saved_by_owners_read_by_everyone(acc, adm, mail):  # noqa: F811
    _owner_and_member(acc, adm)
    assert app(acc, "bo@acme.com", "acme", "GET", "gpu-prices")[1]["prices"] == []
    prices = [{"pattern": "A100.*80", "label": "A100 80GB (reserved)", "usd_per_hour": 2.1}, {"pattern": "T4\\b", "usd_per_hour": 0.35}]
    status, out = app(acc, "ana@acme.com", "acme", "PUT", "gpu-prices", {"prices": prices})
    assert status == 200 and out["prices"] == [prices[0], {"pattern": "T4\\b", "label": "T4\\b", "usd_per_hour": 0.35}]
    got = app(acc, "bo@acme.com", "acme", "GET", "gpu-prices")[1]
    assert got["prices"] == out["prices"] and got["updated_by"] == "ana@acme.com"
    assert app(acc, "bo@acme.com", "acme", "PUT", "gpu-prices", {"prices": []})[0] == 403


@pytest.mark.parametrize("price,error", [
    ({"pattern": "", "usd_per_hour": 1}, "pattern"),
    ({"pattern": "A100[", "usd_per_hour": 1}, "pattern"),
    ({"pattern": "A100(", "usd_per_hour": 1}, "not a valid pattern"),
    ({"pattern": "x" * 65, "usd_per_hour": 1}, "pattern"),
    ({"pattern": "A100", "usd_per_hour": 0}, "usd_per_hour"),
    ({"pattern": "A100", "usd_per_hour": "2"}, "usd_per_hour"),
    ({"pattern": "A100", "usd_per_hour": True}, "usd_per_hour"),
    ({"pattern": "A100", "usd_per_hour": 5000}, "usd_per_hour"),
])
def test_bad_gpu_prices_refused(acc, adm, mail, price, error):  # noqa: F811
    _owner_and_member(acc, adm)
    status, out = app(acc, "ana@acme.com", "acme", "PUT", "gpu-prices", {"prices": [price]})
    assert status == 400 and error in out["error"]
    assert app(acc, "ana@acme.com", "acme", "PUT", "gpu-prices", {"prices": [{"pattern": "a", "usd_per_hour": 1}] * 31})[0] == 400


def test_meters_per_day(acc, adm, mail):  # noqa: F811
    _owner_and_member(acc, adm)
    t = boto3.resource("dynamodb").Table("obs-tenants")
    today = adm._now().strftime("%Y-%m-%d")
    t.put_item(Item={"pk": f"meter#acme#{today}", "tenant": "acme", "day": today, "records": 10, "in_logs": 10, "dropped_logs": 4, "bytes": 99})
    days = app(acc, "bo@acme.com", "acme", "GET", "meters")[1]["days"]
    assert len(days) == 2 and days[0] == {"day": today, "records": 10, "in_logs": 10, "dropped_logs": 4, "bytes": 99}
    assert days[1]["day"] < today and set(days[1]) == {"day"}
    assert app(acc, "ana@acme.com", "acme")[1]["today"]["dropped_logs"] == 4


def test_playground_session_and_read_only_viewer(acc, adm, monkeypatch):  # noqa: F811
    """The public Playground: a visitor gets, with no sign-up, only an ID token for the demo tenant's
    viewer, who can look but not change anything, and sees no one's email or keys."""
    call(adm, "create", tenant="demo-shop")
    call(adm, "invite-user", tenant="demo-shop", email="owner@shop.com", role="owner", send_email=False)
    call(adm, "invite-user", tenant="demo-shop", email="demo@leasyd.com", role="viewer", send_email=False)
    asked = []

    class Ssm:
        def get_parameter(self, Name, WithDecryption):
            return {"Parameter": {"Value": "the-pw"}}

    class Cognito:
        def initiate_auth(self, **kw):
            asked.append(kw)
            return {"AuthenticationResult": {"IdToken": "id.tok", "AccessToken": "acc.tok", "RefreshToken": "ref.tok",
                                             "ExpiresIn": 900}}
    monkeypatch.setattr(acc, "ssm", Ssm())
    monkeypatch.setattr(acc, "cognito", Cognito())

    def session(ip="203.0.113.9"):
        out = acc.handler({"resource": "/v1/demo/session", "httpMethod": "POST", "headers": {"X-Forwarded-For": ip}}, None)
        return out["statusCode"], json.loads(out["body"])
    assert session()[0] == 404                                          # no demo configured
    monkeypatch.setattr(acc, "DEMO_USER", "demo@leasyd.com")
    monkeypatch.setattr(acc, "APP_CLIENT_ID", "client-1")
    assert session() == (200, {"id_token": "id.tok", "expires_in": 900})   # never the access or refresh token
    assert asked[0]["AuthParameters"] == {"USERNAME": "demo@leasyd.com", "PASSWORD": "the-pw"}
    monkeypatch.setattr(acc, "DEMO_SESSIONS_PER_ADDRESS", 2)
    assert session()[0] == 200 and session()[0] == 429                    # per address an hour
    assert session("198.51.100.4")[0] == 200

    status, me = app(acc, "demo@leasyd.com", "demo-shop")
    assert status == 200 and me["you"]["role"] == "viewer" and me["users"] == [] and me["keys"] == []
    assert app(acc, "demo@leasyd.com", "demo-shop", "GET", "drop-rules")[0] == 200
    for method, path, body in [("POST", "keys", {"scope": "ingest"}), ("POST", "users", {"email": "x@y.com"}),
                               ("PUT", "drop-rules", {"rules": []}), ("POST", "billing/checkout", {}),
                               ("DELETE", "users/owner%40shop.com", None)]:
        assert app(acc, "demo@leasyd.com", "demo-shop", method, path, body) == (403, {"error": acc.VIEWER_REFUSED})
    assert len(app(acc, "owner@shop.com", "demo-shop")[1]["users"]) == 2  # owners still see everyone


def test_keys_can_have_names(acc, adm):  # noqa: F811
    """A key may be named ("prod ECS") when created; the list shows the name. Names are trimmed,
    at most 60 characters, and plain text."""
    call(adm, "create", tenant="acme")
    call(adm, "invite-user", tenant="acme", email="bo@acme.com", role="owner", send_email=False)
    status, made = app(acc, "bo@acme.com", "acme", "POST", "keys", {"scope": "ingest", "name": "  prod ECS  "})
    assert status == 201 and made["name"] == "prod ECS"
    assert app(acc, "bo@acme.com", "acme", "POST", "keys", {"scope": "read"})[0] == 201   # a name is optional
    keys = {k["key_id"]: k for k in app(acc, "bo@acme.com", "acme")[1]["keys"]}
    assert keys[made["key_id"]]["name"] == "prod ECS"
    assert sum(1 for k in keys.values() if "name" not in k or not k["name"]) >= 1
    for bad in ["x" * 61, "line\nbreak", 5]:
        status, out = app(acc, "bo@acme.com", "acme", "POST", "keys", {"scope": "ingest", "name": bad})
        assert status == 400 and "name" in out["error"], (bad, status, out)


def test_shared_report_is_a_snapshot_behind_an_expiring_link(acc, adm, monkeypatch):  # noqa: F811
    """A member shares a report: the public link returns exactly that snapshot (no sign-in), nothing
    for a made-up id, and nothing once it expires. A read-only viewer can't share."""
    call(adm, "create", tenant="acme")
    call(adm, "invite-user", tenant="acme", email="ana@acme.com", role="member", send_email=False)
    call(adm, "invite-user", tenant="acme", email="view@acme.com", role="viewer", send_email=False)

    def public(rid):
        out = acc.handler({"resource": "/v1/reports/{id}", "httpMethod": "GET", "pathParameters": {"id": rid}}, None)
        return out["statusCode"], json.loads(out["body"])
    report = {"version": 1, "company": "Acme", "fixes": ["checkout: 3% of requests failing"]}
    status, made = app(acc, "ana@acme.com", "acme", "POST", "reports", {"report": report})
    assert status == 201 and len(made["id"]) >= 20
    status, got = public(made["id"])
    assert status == 200 and got["report"] == report and got["expires_at"] == made["expires_at"]
    assert public("x" * 24)[0] == 404 and public("../tenant#acme")[0] == 404
    assert app(acc, "view@acme.com", "acme", "POST", "reports", {"report": report})[0] == 403
    assert app(acc, "ana@acme.com", "acme", "POST", "reports", {"report": "no"})[0] == 400
    big = {"x": "y" * (acc.REPORT_MAX_BYTES + 1)}
    assert app(acc, "ana@acme.com", "acme", "POST", "reports", {"report": big})[0] == 400
    later = acc._now() + acc.timedelta(days=acc.REPORT_DAYS + 1)
    monkeypatch.setattr(acc, "_now", lambda: later)
    assert public(made["id"])[0] == 404
