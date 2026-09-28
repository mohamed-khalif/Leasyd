"""Tenant operations against moto (API Gateway, Firehose, DynamoDB, S3).
The real-AWS version is infra/phaseT5-test.sh."""

import os
import types

import boto3
import pytest
from moto import mock_aws

os.environ.update(
    TENANTS_TABLE="obs-tenants", INDEX_TABLE="obs-index", USAGE_TABLE="obs-usage", BUCKET="obs-data-test",
    FIREHOSE_ROLE_ARN="arn:aws:iam::123456789012:role/obs-firehose", AWS_DEFAULT_REGION="us-east-1",
    AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", SETTLE_MINUTES="20",
)
os.environ.pop("AWS_SESSION_TOKEN", None)

CTX = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 900_000)


@pytest.fixture
def adm(monkeypatch):
    with mock_aws():
        apigw = boto3.client("apigateway")
        api = apigw.create_rest_api(name="obs-ingest")["id"]
        root = apigw.get_resources(restApiId=api)["items"][0]["id"]
        apigw.put_method(restApiId=api, resourceId=root, httpMethod="POST", authorizationType="NONE")
        apigw.put_integration(restApiId=api, resourceId=root, httpMethod="POST", type="MOCK")
        apigw.create_deployment(restApiId=api, stageName="ingest")
        plan = apigw.create_usage_plan(name="obs-standard", apiStages=[{"apiId": api, "stage": "ingest"}])["id"]
        monkeypatch.setenv("USAGE_PLANS", f'{{"standard": "{plan}"}}')
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "tenant", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "by-tenant", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "tenant", "KeyType": "HASH"},
                                                   {"AttributeName": "pk", "KeyType": "RANGE"}]}])
        for name, (h, r) in {"obs-index": ("pk", "sk"), "obs-usage": ("tenant", "sk")}.items():
            ddb.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": h, "AttributeType": "S"},
                                      {"AttributeName": r, "AttributeType": "S"}],
                KeySchema=[{"AttributeName": h, "KeyType": "HASH"}, {"AttributeName": r, "KeyType": "RANGE"}])
        boto3.client("s3").create_bucket(Bucket="obs-data-test")
        pool = boto3.client("cognito-idp").create_user_pool(
            PoolName="obs-users", UsernameAttributes=["email"],
            Schema=[{"Name": "tenant", "AttributeDataType": "String", "Mutable": False}])["UserPool"]["Id"]
        monkeypatch.setenv("USER_POOL_ID", pool)
        import importlib
        import admin
        importlib.reload(admin)
        invoked = []
        monkeypatch.setattr(admin, "_invoke_self", invoked.append)
        admin.invoked = invoked
        yield admin


def call(adm, action, **kw):
    return adm.handler({"action": action, **kw}, CTX)


def key_status(adm, tenant):
    return {k["api_key_id"]: k["status"] for k in call(adm, "status", tenant=tenant)["keys"]}


def gateway_key(key_id):
    try:
        return boto3.client("apigateway").get_api_key(apiKey=key_id)["enabled"]
    except boto3.client("apigateway").exceptions.NotFoundException:
        return None


def streams():
    return sorted(boto3.client("firehose").list_delivery_streams()["DeliveryStreamNames"])


def test_create_makes_streams_record_and_hashed_key(adm):
    out = call(adm, "create", tenant="acme")
    assert out["api_key"].startswith("obs_") and len(out["api_key"]) == 44
    assert streams() == ["obs-t-acme-logs", "obs-t-acme-metrics", "obs-t-acme-traces"]
    cfg = boto3.client("firehose").describe_delivery_stream(DeliveryStreamName="obs-t-acme-logs")
    dest = cfg["DeliveryStreamDescription"]["Destinations"][0]["ExtendedS3DestinationDescription"]
    assert dest["Prefix"].startswith("_incoming/tenant=acme/logs/dt=")
    st = call(adm, "status", tenant="acme")
    assert st["status"] == "active" and st["plan"] == "standard"
    assert [k["status"] for k in st["keys"]] == ["active"]
    items = boto3.client("dynamodb").scan(TableName="obs-tenants")["Items"]
    assert not any(out["api_key"] in str(i) for i in items)  # only the hash is stored
    assert gateway_key(out["key_id"]) is True
    usage_keys = boto3.client("apigateway").get_usage_plan_keys(usagePlanId=adm.PLANS["standard"])["items"]
    assert [k["id"] for k in usage_keys] == [out["key_id"]]


def buffer_seconds(stream):
    d = boto3.client("firehose").describe_delivery_stream(DeliveryStreamName=stream)["DeliveryStreamDescription"]
    return d["Destinations"][0]["ExtendedS3DestinationDescription"]["BufferingHints"]["IntervalInSeconds"]


def test_tune_sets_the_buffer_of_every_stream(adm):
    call(adm, "create", tenant="acme")
    call(adm, "create", tenant="big", buffer_seconds=15)
    assert buffer_seconds("obs-t-acme-logs") == adm.BUFFER_SECONDS
    assert {buffer_seconds(f"obs-t-big-{s}") for s in ("logs", "traces", "metrics")} == {15}
    assert call(adm, "tune", tenant="acme", buffer_seconds=10) == {"tenant": "acme", "buffer_seconds": 10}
    assert {buffer_seconds(f"obs-t-acme-{s}") for s in ("logs", "traces", "metrics")} == {10}
    assert call(adm, "status", tenant="acme")["buffer_seconds"] == 10
    assert "0-900" in call(adm, "tune", tenant="acme", buffer_seconds=901)["error"]
    assert "not an active tenant" in call(adm, "tune", tenant="nobody", buffer_seconds=10)["error"]


def test_read_keys_have_their_own_scope_and_rotation(adm):
    ingest = call(adm, "create", tenant="acme")
    read = call(adm, "read-key", tenant="acme")
    assert read["scope"] == "read" and read["api_key"].startswith("obs_")
    keys = {k["api_key_id"]: k for k in call(adm, "status", tenant="acme")["keys"]}
    assert keys[ingest["key_id"]]["scope"] == "ingest" and keys[read["key_id"]]["scope"] == "read"
    # Rotating the ingest key leaves the read key alone, and the other way round.
    call(adm, "rotate", tenant="acme", grace_hours=1)
    assert key_status(adm, "acme")[read["key_id"]] == "active"
    assert key_status(adm, "acme")[ingest["key_id"]] == "expiring"
    call(adm, "rotate", tenant="acme", grace_hours=1, scope="read")
    assert key_status(adm, "acme")[read["key_id"]] == "expiring"
    assert "unknown scope" in call(adm, "rotate", tenant="acme", scope="admin")["error"]
    assert "not an active tenant" in call(adm, "read-key", tenant="nobody")["error"]


def test_create_twice_refused_and_bad_input(adm):
    call(adm, "create", tenant="acme")
    assert "already exists" in call(adm, "create", tenant="acme")["error"]
    assert "unknown plan" in call(adm, "create", tenant="beta", plan="gold")["error"]
    with pytest.raises(ValueError):
        call(adm, "create", tenant="Bad#Id")
    assert "unknown action" in call(adm, "explode", tenant="acme")["error"]


def test_rotate_keeps_old_key_for_grace_then_sweep_revokes(adm, monkeypatch):
    old = call(adm, "create", tenant="acme")
    new = call(adm, "rotate", tenant="acme", grace_hours=1)
    assert new["old_key_ids"] == [old["key_id"]] and new["api_key"] != old["api_key"]
    assert key_status(adm, "acme") == {old["key_id"]: "expiring", new["key_id"]: "active"}
    assert call(adm, "sweep")["expired_keys"] == []  # grace not over
    later = adm._now() + adm.timedelta(hours=2)
    monkeypatch.setattr(adm, "_now", lambda: later)
    assert call(adm, "sweep")["expired_keys"] == [old["key_id"]]
    assert key_status(adm, "acme") == {old["key_id"]: "revoked", new["key_id"]: "active"}
    assert gateway_key(old["key_id"]) is False and gateway_key(new["key_id"]) is True


def test_revoke_one_or_all(adm):
    a = call(adm, "create", tenant="acme")
    b = call(adm, "rotate", tenant="acme", grace_hours=0.5)
    assert call(adm, "revoke", tenant="acme", key_id=b["key_id"])["revoked"] == [b["key_id"]]
    assert key_status(adm, "acme") == {a["key_id"]: "expiring", b["key_id"]: "revoked"}
    assert call(adm, "revoke", tenant="acme")["revoked"] == [a["key_id"]]
    assert set(key_status(adm, "acme").values()) == {"revoked"}
    assert "no live key" in call(adm, "revoke", tenant="acme", key_id="nope")["error"]


def seed_data(tenant):
    s3 = boto3.client("s3")
    for k in (f"_incoming/tenant={tenant}/logs/dt=2026-09-26/hour=10/a.json.gz",
              f"_incoming/_errors/tenant={tenant}/logs/x/dt=2026-09-26/e.gz",
              f"data/tenant={tenant}/logs/dt=2026-09-26/hour=10/service=api/part-1-000.parquet",
              f"data/tenant={tenant}/traces/_bloom/b.bloom"):
        s3.put_object(Bucket="obs-data-test", Key=k, Body=b"x")
    ddb = boto3.client("dynamodb")
    for pk, sk in ((f"{tenant}#logs#api", "2026-09-26T10:00:00.000000Z#b"), (f"{tenant}#_services#logs", "all"),
                   (f"{tenant}#_plan#logs#2026-09-26#10", "b"), (f"{tenant}#_raw#logs#2026-09-26#10", "k"),
                   (f"_lease#{tenant}#logs#2026-09-26#10#b", "lease")):
        ddb.put_item(TableName="obs-index", Item={"pk": {"S": pk}, "sk": {"S": sk}})
    ddb.put_item(TableName="obs-usage", Item={"tenant": {"S": tenant}, "sk": {"S": "2026-09-26#logs#b"},
                                              "dt": {"S": "2026-09-26"}, "signal": {"S": "logs"},
                                              "records": {"N": "5"}})


def remaining(tenant):
    objs = boto3.client("s3").list_objects_v2(Bucket="obs-data-test").get("Contents", [])
    items = boto3.client("dynamodb").scan(TableName="obs-index")["Items"]
    return ([o["Key"] for o in objs if f"tenant={tenant}/" in o["Key"]],
            [i["pk"]["S"] for i in items if i["pk"]["S"].startswith(f"{tenant}#")
             or i["pk"]["S"].startswith(f"_lease#{tenant}#")])


def test_delete_purges_only_that_tenant_and_finishes_after_a_clean_pass(adm, monkeypatch):
    a = call(adm, "create", tenant="acme")
    call(adm, "create", tenant="acme-2")  # shares the prefix "acme": must survive
    seed_data("acme")
    seed_data("acme-2")
    out = call(adm, "delete", tenant="acme")
    assert out["status"] == "deleting" and out["revoked"] == [a["key_id"]]
    assert gateway_key(a["key_id"]) is None  # removed from API Gateway
    assert streams() == ["obs-t-acme-2-logs", "obs-t-acme-2-metrics", "obs-t-acme-2-traces"]
    assert adm.invoked == [{"action": "purge", "tenant": "acme"}]
    assert "being deleted" in call(adm, "create", tenant="acme")["error"]

    assert call(adm, "purge", tenant="acme")["deleted"] == 9
    assert remaining("acme") == ([], [])
    assert len(remaining("acme-2")[0]) == 4 and len(remaining("acme-2")[1]) == 5
    # A worker that was mid-flight writes after the first pass...
    boto3.client("s3").put_object(Bucket="obs-data-test", Key="data/tenant=acme/logs/late.parquet", Body=b"x")
    assert call(adm, "sweep")["purging"] == []  # not due yet
    t = [adm._now()]
    monkeypatch.setattr(adm, "_now", lambda: t[0])
    t[0] += adm.timedelta(minutes=21)
    assert call(adm, "sweep")["purging"] == ["acme"]
    assert call(adm, "purge", tenant="acme")["deleted"] == 1  # ...and the second pass catches it
    t[0] += adm.timedelta(minutes=21)
    assert call(adm, "sweep")["purging"] == ["acme"]
    assert call(adm, "purge", tenant="acme")["deleted"] == 0
    assert call(adm, "sweep")["deleted"] == ["acme"]
    assert call(adm, "status", tenant="acme")["status"] == "deleted"
    assert call(adm, "usage", tenant="acme", start="2026-09-01", end="2026-09-30")["totals"]["logs"]["records"] == 5
    # the id can be reused once deleted
    assert "api_key" in call(adm, "create", tenant="acme")


def test_purge_continues_when_out_of_time(adm, monkeypatch):
    call(adm, "create", tenant="acme")
    seed_data("acme")
    call(adm, "delete", tenant="acme")
    adm.invoked.clear()
    short = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 30_000)  # already past the deadline
    out = adm.handler({"action": "purge", "tenant": "acme"}, short)
    assert out["continuing"] is True and adm.invoked == [{"action": "purge", "tenant": "acme", "deleted": 0}]
    assert call(adm, "purge", tenant="acme", deleted=0)["deleted"] == 9


def test_delete_legacy_tenant_without_record(adm):
    """Tenants created before T5 have keys but no tenant record."""
    key, key_id = adm._issue_key("old", "standard")
    out = call(adm, "delete", tenant="old")
    assert out["status"] == "deleting" and out["revoked"] == [key_id]
    assert "no tenant" in call(adm, "delete", tenant="never")["error"]


def test_usage_sums_per_day_and_signal(adm):
    ddb = boto3.client("dynamodb")
    rows = [("2026-09-25", "logs", "a", 10, 100, 50), ("2026-09-26", "logs", "b", 5, 70, 30),
            ("2026-09-26", "logs", "c", 1, 10, 5), ("2026-09-26", "traces", "d", 7, 80, 40),
            ("2026-10-02", "logs", "e", 99, 1, 1)]
    for dt, sig, b, rec, raw, stored in rows:
        ddb.put_item(TableName="obs-usage", Item={
            "tenant": {"S": "acme"}, "sk": {"S": f"{dt}#{sig}#{b}"}, "dt": {"S": dt}, "signal": {"S": sig},
            "records": {"N": str(rec)}, "raw_bytes": {"N": str(raw)}, "stored_bytes": {"N": str(stored)},
            "files": {"N": "1"}})
    out = call(adm, "usage", tenant="acme", start="2026-09-25", end="2026-09-26")
    assert out["days"]["2026-09-26"]["logs"] == {"records": 6, "raw_bytes": 80, "stored_bytes": 35, "files": 2}
    assert out["totals"] == {"logs": {"records": 16, "raw_bytes": 180, "stored_bytes": 85, "files": 3},
                             "traces": {"records": 7, "raw_bytes": 80, "stored_bytes": 40, "files": 1}}
    assert call(adm, "usage", tenant="other")["totals"] == {}


def test_list(adm):
    call(adm, "create", tenant="beta")
    call(adm, "create", tenant="acme")
    assert [t["tenant"] for t in call(adm, "list")["tenants"]] == ["acme", "beta"]


def login(email):
    try:
        u = boto3.client("cognito-idp").admin_get_user(UserPoolId=os.environ["USER_POOL_ID"], Username=email)
    except boto3.client("cognito-idp").exceptions.UserNotFoundException:
        return None
    return {a["Name"]: a["Value"] for a in u["UserAttributes"]}


def test_invite_list_remove_users(adm):
    call(adm, "create", tenant="acme")
    call(adm, "create", tenant="beta")
    out = call(adm, "invite-user", tenant="acme", email=" Ana@Example.com ", send_email=False)
    assert out == {"tenant": "acme", "email": "ana@example.com", "status": "invited", "email_sent": False}
    assert login("ana@example.com")["custom:tenant"] == "acme"
    # one login per email, and only for an active tenant
    assert "already a user of this tenant" in call(adm, "invite-user", tenant="acme", email="ana@example.com")["error"]
    assert "another tenant" in call(adm, "invite-user", tenant="beta", email="ana@example.com")["error"]
    assert "not an email" in call(adm, "invite-user", tenant="acme", email="nope")["error"]
    assert "not an active tenant" in call(adm, "invite-user", tenant="gone", email="x@example.com")["error"]
    assert [u["email"] for u in call(adm, "users", tenant="acme")["users"]] == ["ana@example.com"]
    assert call(adm, "users", tenant="beta")["users"] == []
    # another tenant can't remove acme's user
    assert "not a user of beta" in call(adm, "remove-user", tenant="beta", email="ana@example.com")["error"]
    assert call(adm, "remove-user", tenant="acme", email="ana@example.com")["status"] == "removed"
    assert login("ana@example.com") is None
    assert call(adm, "users", tenant="acme")["users"][0]["status"] == "removed"
    # removed users can be invited again (e.g. to another tenant)
    assert call(adm, "invite-user", tenant="beta", email="ana@example.com", send_email=False)["status"] == "invited"
    assert login("ana@example.com")["custom:tenant"] == "beta"


def test_deleting_a_tenant_removes_its_logins(adm):
    call(adm, "create", tenant="acme")
    call(adm, "invite-user", tenant="acme", email="a@example.com", send_email=False)
    call(adm, "invite-user", tenant="acme", email="b@example.com", send_email=False)
    call(adm, "delete", tenant="acme")
    assert login("a@example.com") is None and login("b@example.com") is None
    assert {u["status"] for u in call(adm, "users", tenant="acme")["users"]} == {"removed"}
