"""API Gateway authorizer: API key -> tenant.

Customers' OpenTelemetry SDKs send `x-api-key: <key>`. The key's SHA-256 is
looked up in the obs-tenants table (keys themselves are never stored). On a
match the request is allowed, the tenant is passed to the ingest Lambda in
requestContext.authorizer (which only the authorizer can set), and the key is
returned as usageIdentifierKey so API Gateway applies the tenant's usage plan
(rate limits and quotas).

A key is accepted while its status is "active", or "expiring" (replaced by a
rotation) until its expires_at.

Each key has a scope (keys created before scopes existed are "ingest"):
  ingest  POST /v1/logs, /v1/traces, /v1/metrics   (the key in customers' SDKs)
  read    POST /v1/query, GET /v1/query/{job}    (dashboards, scripts, the UI)
so a key embedded in an application can send data but never read it back.

API Gateway caches the answer per key for 60 s, so a revoked key is refused
within that; disabling the key in API Gateway (infra/tenant.sh revoke does
both) usually refuses it sooner. New keys take up to ~10 minutes to reach every
API Gateway node (measured 2026-09-28) and are refused (403) by some requests
until then.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

import boto3

TABLE = os.environ["TENANTS_TABLE"]
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

ROUTES = {"ingest": ["POST/v1/logs", "POST/v1/traces", "POST/v1/metrics"], "read": ["POST/v1/query", "GET/v1/query/*"]}

ddb = boto3.client("dynamodb")


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _live(item):
    status = item.get("status", {}).get("S")
    if status == "active":
        return True
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return status == "expiring" and item.get("expires_at", {}).get("S", "") > now


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    key = (headers.get("x-api-key") or "").strip()
    if not key or len(key) > 256:
        raise Exception("Unauthorized")  # API Gateway turns exactly this into a 401

    item = ddb.get_item(TableName=TABLE, Key={"pk": {"S": f"key#{key_hash(key)}"}}).get("Item")
    if not item or not _live(item):
        print(json.dumps({"denied": "unknown or inactive key"}))
        raise Exception("Unauthorized")
    tenant = item["tenant"]["S"]
    scope = item.get("scope", {}).get("S", "ingest")
    if not _TENANT.match(tenant) or scope not in ROUTES:
        raise Exception("Unauthorized")

    # Allow every route of the key's scope in this API stage, so the cached
    # answer (per key) covers them all; other routes get 403.
    arn = event["methodArn"].split("/")      # arn:...:<api-id> / <stage> / <method> / <path...>
    base = "/".join(arn[:2])
    return {
        "principalId": tenant,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "execute-api:Invoke",
                           "Resource": [f"{base}/{route}" for route in ROUTES[scope]]}],
        },
        "context": {"tenant": tenant, "scope": scope},
        "usageIdentifierKey": key,
    }
