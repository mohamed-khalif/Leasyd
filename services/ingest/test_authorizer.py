import os

import boto3
import pytest
from moto import mock_aws

os.environ.update(TENANTS_TABLE="obs-tenants", AWS_DEFAULT_REGION="us-east-1",
                  AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing")
os.environ.pop("AWS_SESSION_TOKEN", None)

ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123/ingest/POST/v1/logs"


@pytest.fixture
def auth():
    with mock_aws():
        import importlib
        import authorizer
        importlib.reload(authorizer)
        ddb = boto3.client("dynamodb")
        ddb.create_table(TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
        for key, tenant, status, expires in [
                ("good-key", "acme", "active", None), ("old-key", "acme", "revoked", None),
                ("bad-tenant-key", "Acme#x", "active", None),
                ("rotated-key", "acme", "expiring", "2999-01-01T00:00:00Z"),
                ("expired-key", "acme", "expiring", "2020-01-01T00:00:00Z"),
                ("read-key", "acme", "active", None), ("odd-scope-key", "acme", "active", None)]:
            item = {"pk": {"S": f"key#{authorizer.key_hash(key)}"}, "tenant": {"S": tenant}, "status": {"S": status}}
            if expires:
                item["expires_at"] = {"S": expires}
            if key == "read-key":
                item["scope"] = {"S": "read"}
            if key == "odd-scope-key":
                item["scope"] = {"S": "admin"}
            ddb.put_item(TableName="obs-tenants", Item=item)
        yield authorizer


def call(auth, headers):
    return auth.handler({"type": "REQUEST", "methodArn": ARN, "headers": headers}, None)


def test_valid_key_maps_to_tenant(auth):
    out = call(auth, {"X-Api-Key": "good-key"})
    assert out["context"] == {"tenant": "acme", "scope": "ingest"} and out["principalId"] == "acme"
    assert out["usageIdentifierKey"] == "good-key"
    stmt = out["policyDocument"]["Statement"][0]
    assert stmt["Effect"] == "Allow"
    base = "arn:aws:execute-api:us-east-1:123456789012:abc123/ingest"
    assert stmt["Resource"] == [f"{base}/POST/v1/logs", f"{base}/POST/v1/traces", f"{base}/POST/v1/metrics",
                                f"{base}/POST/v1/aws/cloudwatch-metrics"]


def test_firehose_sends_the_key_in_its_own_header(auth):
    out = call(auth, {"X-Amz-Firehose-Access-Key": "good-key"})
    assert out["context"] == {"tenant": "acme", "scope": "ingest"} and out["usageIdentifierKey"] == "good-key"


def test_read_key_may_only_query_and_use_the_mcp_server(auth):
    out = call(auth, {"x-api-key": "read-key"})
    assert out["context"] == {"tenant": "acme", "scope": "read"}
    assert out["policyDocument"]["Statement"][0]["Resource"] == [
        "arn:aws:execute-api:us-east-1:123456789012:abc123/ingest/POST/v1/query",
        "arn:aws:execute-api:us-east-1:123456789012:abc123/ingest/GET/v1/query/*",   # a long query's answer
        "arn:aws:execute-api:us-east-1:123456789012:abc123/ingest/*/v1/mcp"]


@pytest.mark.parametrize("headers", [{}, {"x-api-key": ""}, {"x-api-key": "nope"}, {"x-api-key": "old-key"}, {"x-api-key": "expired-key"},
                                     {"x-api-key": "bad-tenant-key"}, {"x-api-key": "x" * 300}, {"x-api-key": "odd-scope-key"},
                                     {"authorization": "good-key"}])
def test_rejected(auth, headers):
    with pytest.raises(Exception, match="^Unauthorized$"):
        call(auth, headers)


def test_keys_are_stored_hashed(auth):
    items = boto3.client("dynamodb").scan(TableName="obs-tenants")["Items"]
    assert not any("good-key" in str(i) for i in items)


def test_rotated_key_works_until_it_expires(auth):
    assert call(auth, {"x-api-key": "rotated-key"})["context"] == {"tenant": "acme", "scope": "ingest"}
