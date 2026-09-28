"""API Gateway authorizer: API key -> tenant.

Customers' OpenTelemetry SDKs send `x-api-key: <key>`. The key's SHA-256 is
looked up in the obs-tenants table (keys themselves are never stored). On a
match the request is allowed, the tenant is passed to the ingest Lambda in
requestContext.authorizer (which only the authorizer can set), and the key is
returned as usageIdentifierKey so API Gateway applies the tenant's usage plan
(rate limits and quotas).

A key is accepted while its status is "active", or "expiring" (replaced by a
rotation) until its expires_at.

API Gateway caches the answer per key for 60 s, so a revoked key is refused
within that; disabling the key in API Gateway (infra/tenant.sh revoke does
both) usually refuses it sooner. New keys take about a minute to reach every
API Gateway node, and are refused (403) until then.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

import boto3

TABLE = os.environ["TENANTS_TABLE"]
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

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
    if not _TENANT.match(tenant):
        raise Exception("Unauthorized")

    # Allow every method of this API stage, so the cached answer covers
    # /v1/logs, /v1/traces and /v1/metrics alike.
    arn = event["methodArn"].split("/")
    resource = "/".join(arn[:2]) + "/*"
    return {
        "principalId": tenant,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "execute-api:Invoke", "Resource": resource}],
        },
        "context": {"tenant": tenant},
        "usageIdentifierKey": key,
    }
