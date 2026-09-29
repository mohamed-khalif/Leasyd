#!/usr/bin/env python3
"""End-to-end test of customer logins (U1) on AWS.

    python3 infra/login-test.py [--tenant t6-000] [--other t6-001]

Invites a test user to --tenant (no email sent; the test sets its password),
signs in like the web app does (Cognito, USER_PASSWORD_AUTH), and checks:
  - GET /v1/app/me names the user's tenant;
  - POST /v1/app/query answers like the query engine, for that tenant only
    (a tenant named in the body is ignored);
  - no token, a forged token, or an API key -> 401 on /v1/app/*;
  - the user cannot change their own tenant attribute;
  - after remove-user, the user can neither sign in nor refresh.
"""

import argparse
import base64
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

FAILED = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        FAILED.append(msg)


def call(url, method="GET", token=None, body=None, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = token
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read() or b"{}"), time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}"), time.time() - t0
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}, time.time() - t0


def outputs(cf, stack):
    return {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", default="t6-000")
    p.add_argument("--other", default="t6-001")
    a = p.parse_args()
    cf, lam, idp = boto3.client("cloudformation"), boto3.client("lambda"), boto3.client("cognito-idp")
    lam = boto3.client("lambda", config=__import__("botocore.config").config.Config(read_timeout=300))
    u1 = outputs(cf, "obs-phaseU1")
    endpoint = outputs(cf, "obs-phaseT2")["IngestEndpoint"]

    def admin(payload):
        return json.loads(lam.invoke(FunctionName="obs-tenant-admin", Payload=json.dumps(payload).encode())["Payload"].read())

    email = f"login-test-{secrets.token_hex(4)}@example.com"
    password = "Aa1-" + secrets.token_urlsafe(18)
    refresh = None
    out = admin({"action": "invite-user", "tenant": a.tenant, "email": email, "send_email": False})
    check(out.get("status") == "invited", f"invite-user {email} to {a.tenant}: {out}")
    try:
        idp.admin_set_user_password(UserPoolId=u1["UserPoolId"], Username=email, Password=password, Permanent=True)
        auth = idp.initiate_auth(ClientId=u1["AppClientId"], AuthFlow="USER_PASSWORD_AUTH",
                                 AuthParameters={"USERNAME": email, "PASSWORD": password})["AuthenticationResult"]
        id_token, access, refresh = auth["IdToken"], auth["AccessToken"], auth["RefreshToken"]
        check(True, "signed in (Cognito)")

        status, me, _ = call(f"{endpoint}/v1/app/me", token=id_token)
        check(status == 200 and me == {"tenant": a.tenant, "email": email}, f"GET /v1/app/me -> {status} {me}")

        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
        q = {"signal": "logs", "start": iso(now - timedelta(hours=24)), "end": iso(now),
             "group_by": ["service"], "aggs": [{"fn": "count"}]}
        engine = {t: sorted(json.loads(lam.invoke(FunctionName="obs-query", Payload=json.dumps({**q, "tenant": t})
                                                  .encode())["Payload"].read())["rows"]) for t in (a.tenant, a.other)}
        status, out, secs = call(f"{endpoint}/v1/app/query", "POST", id_token, q)
        check(status == 200 and sorted(out.get("rows", [])) == engine[a.tenant] and engine[a.tenant],
              f"POST /v1/app/query: {len(out.get('rows', []))} services, same counts as the engine, {secs:.2f}s")
        status, out, _ = call(f"{endpoint}/v1/app/query", "POST", id_token, {**q, "tenant": a.other})
        check(status == 200 and sorted(out.get("rows", [])) == engine[a.tenant] != engine[a.other],
              f"a tenant named in the body ({a.other}) is ignored (HTTP {status})")

        status, _, _ = call(f"{endpoint}/v1/app/query", "POST", None, q)
        check(status == 401, f"no token -> {status}")
        head, payload, sig = id_token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["custom:tenant"] = a.other
        forged = ".".join([head, base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("="), sig])
        status, _, _ = call(f"{endpoint}/v1/app/query", "POST", forged, q)
        check(status in (401, 403), f"token edited to claim {a.other} -> refused ({status})")
        status, _, _ = call(f"{endpoint}/v1/app/me", headers={"x-api-key": "obs_notauserkey"})
        check(status == 401, f"API key instead of a login -> {status}")

        try:
            idp.update_user_attributes(AccessToken=access, UserAttributes=[{"Name": "custom:tenant", "Value": a.other}])
            check(False, "user changed their own tenant attribute")
        except idp.exceptions.NotAuthorizedException:
            check(True, "user cannot change their own tenant attribute")
    finally:
        out = admin({"action": "remove-user", "tenant": a.tenant, "email": email})
        print(f"removed {email}: {out}")
    try:
        if refresh is None:
            raise idp.exceptions.NotAuthorizedException({"Error": {"Code": "x", "Message": "never signed in"}}, "x")
        idp.initiate_auth(ClientId=u1["AppClientId"], AuthFlow="REFRESH_TOKEN_AUTH",
                          AuthParameters={"REFRESH_TOKEN": refresh})
        check(False, "refresh still works after remove-user")
    except (idp.exceptions.NotAuthorizedException, idp.exceptions.UserNotFoundException):
        check(True, "after remove-user the refresh token is refused")
    try:
        idp.initiate_auth(ClientId=u1["AppClientId"], AuthFlow="USER_PASSWORD_AUTH",
                          AuthParameters={"USERNAME": email, "PASSWORD": password})
        check(False, "sign-in still works after remove-user")
    except (idp.exceptions.NotAuthorizedException, idp.exceptions.UserNotFoundException):
        check(True, "after remove-user the user cannot sign in")
    print(f"{len(FAILED)} failed" if FAILED else "all passed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
