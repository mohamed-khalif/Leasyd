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
                ("expired-key", "acme", "expiring", "2020-01-01T00:00:00Z")]:
            item = {"pk": {"S": f"key#{authorizer.key_hash(key)}"}, "tenant": {"S": tenant}, "status": {"S": status}}
            if expires:
                item["expires_at"] = {"S": expires}
            ddb.put_item(TableName="obs-tenants", Item=item)
        yield authorizer


def call(auth, headers):
    return auth.handler({"type": "REQUEST", "methodArn": ARN, "headers": headers}, None)


def test_valid_key_maps_to_tenant(auth):
    out = call(auth, {"X-Api-Key": "good-key"})
    assert out["context"] == {"tenant": "acme"} and out["principalId"] == "acme"
    assert out["usageIdentifierKey"] == "good-key"
    stmt = out["policyDocument"]["Statement"][0]
    assert stmt["Effect"] == "Allow"
    assert stmt["Resource"] == "arn:aws:execute-api:us-east-1:123456789012:abc123/ingest/*"


@pytest.mark.parametrize("headers", [{}, {"x-api-key": ""}, {"x-api-key": "nope"}, {"x-api-key": "old-key"}, {"x-api-key": "expired-key"},
                                     {"x-api-key": "bad-tenant-key"}, {"x-api-key": "x" * 300},
                                     {"authorization": "good-key"}])
def test_rejected(auth, headers):
    with pytest.raises(Exception, match="^Unauthorized$"):
        call(auth, headers)


def test_keys_are_stored_hashed(auth):
    items = boto3.client("dynamodb").scan(TableName="obs-tenants")["Items"]
    assert not any("good-key" in str(i) for i in items)


def test_rotated_key_works_until_it_expires(auth):
    assert call(auth, {"x-api-key": "rotated-key"})["context"] == {"tenant": "acme"}
