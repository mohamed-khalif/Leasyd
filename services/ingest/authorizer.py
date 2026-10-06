"""API Gateway authorizer: API key -> tenant.

Customers' OpenTelemetry SDKs send `x-api-key: <key>`; a customer's Firehose (CloudWatch
metric streams, POST /v1/aws/cloudwatch-metrics) sends it as X-Amz-Firehose-Access-Key. The key's SHA-256 is
looked up in the obs-tenants table (keys themselves are never stored). On a
match the request is allowed, the tenant is passed to the ingest Lambda in
requestContext.authorizer (which only the authorizer can set), and the key's
API Gateway key is returned as usageIdentifierKey so API Gateway applies the
tenant's usage plan (rate limits and quotas). That is the gateway key the
customer's key borrowed from a pool of aged keys (gateway_key, see
services/tenants/admin.py KEY_POOL_SIZE), or for older keys the key itself.

A key is accepted while its status is "active", or "expiring" (replaced by a
rotation) until its expires_at.

Each key has a scope (keys created before scopes existed are "ingest"):
  ingest  POST /v1/logs, /v1/traces, /v1/metrics, /v1/aws/cloudwatch-metrics   (customers' SDKs, Firehose)
  read    POST /v1/query, GET /v1/query/{job}    (dashboards, scripts, the UI)
          /v1/mcp                                 (the MCP server, for AI agents)
so a key embedded in an application can send data but never read it back.

API Gateway caches the answer per key for 60 s, so a revoked key is refused
within that; disabling the key in API Gateway (infra/tenant.sh revoke does
both) usually refuses it sooner. A new API Gateway key takes minutes (up to 12+
measured 2026-10-06) to reach every API Gateway node and is refused by some
requests until then: hence the pool of aged gateway keys. Should a request still
meet an unknown gateway key, API Gateway answers 429 with Retry-After
(infra/phaseT2-ingest.yaml InvalidApiKeyResponse), which exporters retry.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

import boto3

TABLE = os.environ["TENANTS_TABLE"]
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

ROUTES = {"ingest": ["POST/v1/logs", "POST/v1/traces", "POST/v1/metrics", "POST/v1/aws/cloudwatch-metrics"], "read": ["POST/v1/query", "GET/v1/query/*", "*/v1/mcp"]}

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
    key = (headers.get("x-api-key") or headers.get("x-amz-firehose-access-key") or "").strip()
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
        "usageIdentifierKey": item.get("gateway_key", {}).get("S") or key,
    }
