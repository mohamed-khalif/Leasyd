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
        free = apigw.create_usage_plan(name="obs-free", apiStages=[{"apiId": api, "stage": "ingest"}])["id"]
        monkeypatch.setenv("USAGE_PLANS", f'{{"standard": "{plan}", "free": "{free}"}}')
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
              f"data/tenant={tenant}/traces/_bloom/b.bloom",
              f"synthetics/tenant={tenant}/abc123abc123/{'0' * 32}/1.jpg"):
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

    assert call(adm, "purge", tenant="acme")["deleted"] == 10
    assert remaining("acme") == ([], [])
    assert len(remaining("acme-2")[0]) == 5 and len(remaining("acme-2")[1]) == 5
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
    assert call(adm, "purge", tenant="acme", deleted=0)["deleted"] == 10


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
    assert out == {"tenant": "acme", "email": "ana@example.com", "role": "owner", "status": "invited", "email_sent": False}
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


def test_restore_puts_live_keys_in_new_plans_and_recreates_streams(adm, monkeypatch):
    """infra/down.sh keeps tenants and keys; infra/up.sh recreates the API with new usage plans."""
    a = call(adm, "create", tenant="acme")
    r = call(adm, "read-key", tenant="acme")
    gone = call(adm, "create", tenant="gone")
    call(adm, "delete", tenant="gone")
    apigw = boto3.client("apigateway")
    api = apigw.get_rest_apis()["items"][0]["id"]
    new_plan = apigw.create_usage_plan(name="obs-standard-2", apiStages=[{"apiId": api, "stage": "ingest"}])["id"]
    monkeypatch.setitem(adm.PLANS, "standard", new_plan)
    boto3.client("firehose").delete_delivery_stream(DeliveryStreamName="obs-t-acme-traces")

    out = call(adm, "restore")
    assert out["tenants"] == ["acme"]
    assert sorted(out["keys_added_to_plans"]) == sorted([a["key_id"], r["key_id"]])
    in_plan = {k["id"] for k in apigw.get_usage_plan_keys(usagePlanId=new_plan)["items"]}
    assert in_plan == {a["key_id"], r["key_id"]} and gone["key_id"] not in in_plan
    assert streams() == ["obs-t-acme-logs", "obs-t-acme-metrics", "obs-t-acme-traces"]
    assert call(adm, "restore")["keys_added_to_plans"] == []   # safe to repeat


def test_deleting_a_tenant_removes_its_synthetic_checks(adm):
    call(adm, "create", tenant="acme")
    call(adm, "create", tenant="globex")
    ddb = boto3.resource("dynamodb").Table("obs-tenants")
    kinds = ("check#{t}#aaaabbbbcccc", "exclude#{t}#aaaabbbbcccc#" + "a" * 32, "window#{t}#bbbbccccdddd", "slo#{t}#ccccddddeeee",
             "alert#{t}#ddddeeeeffff", "astate#{t}#ddddeeeeffff#aaaabbbbcccc", "channel#{t}#eeeeffff0000")
    sns = boto3.client("sns")
    for t in ("acme", "globex"):
        for k in kinds:
            extra = {"topic_arn": sns.create_topic(Name=f"obs-alert-{t}-eeeeffff0000")["TopicArn"]} if k.startswith("channel#") else {}
            ddb.put_item(Item={"pk": k.format(t=t), "tenant": t, "name": "x", "enabled": True, **extra})
    call(adm, "delete", tenant="acme")
    left = {i["pk"] for i in ddb.scan()["Items"] if i["pk"].startswith(("check#", "exclude#", "window#", "slo#", "alert#", "astate#", "channel#"))}
    assert left == {k.format(t="globex") for k in kinds}
    assert [t["TopicArn"].rsplit(":", 1)[1] for t in sns.list_topics()["Topics"]] == ["obs-alert-globex-eeeeffff0000"]


# ------------------------------------------------------------------ retention

def seed_days(tenant):
    """Index entries and objects on 2026-09-14 (before the cutoff) and 2026-09-15 (kept)."""
    s3, ddb = boto3.client("s3"), boto3.client("dynamodb")
    put = lambda k: s3.put_object(Bucket="obs-data-test", Key=k, Body=b"x")   # noqa: E731
    item = lambda pk, sk, **a: ddb.put_item(TableName="obs-index", Item={"pk": {"S": pk}, "sk": {"S": sk}, **{k: {"S": v} for k, v in a.items()}})  # noqa: E731
    ddb.put_item(TableName="obs-index", Item={"pk": {"S": f"{tenant}#_services#logs"}, "sk": {"S": "all"}, "services": {"SS": ["api"]}})
    for day, hour in (("2026-09-14", "10"), ("2026-09-15", "00")):
        f = f"data/tenant={tenant}/logs/dt={day}/hour={hour}/service=api/part-b-000.parquet"
        put(f)
        item(f"{tenant}#logs#api", f"{day}T{hour}:00:00.000000Z#b-000", min_ts=f"{day}T{hour}:00:00.000000Z",
             max_ts=f"{day}T{hour}:59:59.000000Z", file_path=f"s3://obs-data-test/{f}")
        for k in (f"data/tenant={tenant}/logs/_bloom/day/dt={day}/v=1/g=00.bloom", f"data/tenant={tenant}/logs/_bloom/hour/dt={day}/hour={hour}/v=1/g=00.bloom",
                  f"data/tenant={tenant}/logs/_ids/dt={day}/hour={hour}/g=00/b.bin", f"_incoming/tenant={tenant}/logs/dt={day}/hour={hour}/raw.gz"):
            put(k)
        item(f"{tenant}#_day#logs", day)
        item(f"{tenant}#_hour#logs", f"{day}T{hour}")
        item(f"{tenant}#_plan#logs#{day}#{hour}", "b")
        item(f"{tenant}#_raw#logs#{day}#{hour}", "raw.gz")
    # A fast-lane file from before midnight with rows after it: kept (its newest rows are in range).
    put(f"data/tenant={tenant}/logs/_fast/late.parquet")
    item(f"{tenant}#logs#api", "2026-09-14T23:30:00.000000Z#f-000", min_ts="2026-09-14T23:30:00.000000Z",
         max_ts="2026-09-15T00:10:00.000000Z", file_path=f"s3://obs-data-test/data/tenant={tenant}/logs/_fast/late.parquet", kind="raw")
    put(f"synthetics/tenant={tenant}/abc123abc123/{'0' * 32}/1.jpg")


def test_retention_deletes_days_before_the_cutoff_only(adm, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(adm, "_now", lambda: datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc))
    assert adm.retention_cutoff() == "2026-09-15"                       # 30 full days before today are kept
    call(adm, "create", tenant="acme")
    seed_days("acme")
    seed_days("acme-2")                                                 # no tenant record: never swept
    out = call(adm, "retention")
    assert out["cutoff"] == "2026-09-15" and out["tenants"] == ["acme"] and out["deleted"] > 0
    objs = [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="obs-data-test")["Contents"]]
    mine = sorted(k for k in objs if "tenant=acme/" in k)
    assert not [k for k in mine if "2026-09-14" in k], mine
    assert len([k for k in mine if "2026-09-15" in k]) == 5
    assert f"data/tenant=acme/logs/_fast/late.parquet" in mine          # straddles midnight: kept
    assert any(k.startswith("synthetics/") for k in mine)                # screenshots: the lifecycle rule's job
    assert len([k for k in objs if "tenant=acme-2/" in k]) == 12         # the other "tenant" untouched
    items = {(i["pk"]["S"], i["sk"]["S"]) for i in boto3.client("dynamodb").scan(TableName="obs-index")["Items"] if i["pk"]["S"].startswith("acme#")}
    assert not [i for i in items if "2026-09-14" in i[0] + i[1] and "T23:30" not in i[1]], items
    assert ("acme#logs#api", "2026-09-14T23:30:00.000000Z#f-000") in items
    assert {("acme#_day#logs", "2026-09-15"), ("acme#_plan#logs#2026-09-15#00", "b"), ("acme#logs#api", "2026-09-15T00:00:00.000000Z#b-000")} <= items
    assert call(adm, "retention")["deleted"] == 0                        # nothing left to do


def test_retention_carries_on_in_a_new_invocation(adm, monkeypatch):
    for t in ("acme", "beta"):
        call(adm, "create", tenant=t)
    short = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 30_000})()
    out = adm.handler({"action": "retention"}, short)                    # no time left: hand over at once
    assert out["continuing"] and adm.invoked[-1] == {"action": "retention", "after": None}
    out = adm.handler({"action": "retention", "after": "acme"}, CTX)
    assert out["tenants"] == ["beta"]


# ------------------------------------------------------------------ self-service (sign-up, SES emails)

@pytest.fixture
def mail(adm, monkeypatch):
    """SES on: the sending domain verified, emails.EMAIL_FROM set. -> the sent messages."""
    import emails
    boto3.client("ses").verify_domain_identity(Domain="app.leasyd.com")
    monkeypatch.setattr(emails, "EMAIL_FROM", "Leasyd <no-reply@app.leasyd.com>")
    monkeypatch.setattr(emails, "_ses", None)
    from moto.ses.models import ses_backends
    return ses_backends["123456789012"]["us-east-1"].sent_messages


def message(m):
    import html
    return {"to": m.destinations["ToAddresses"], "subject": m.subject, "text": html.unescape(m.body)}


def test_signup_makes_a_free_tenant_and_welcomes_its_owner(adm, mail):
    boto3.resource("dynamodb").Table("obs-tenants").put_item(
        Item={"pk": "tenant#acme-inc", "tenant": "acme-inc", "status": "creating", "company": "Acme Inc"})
    out = call(adm, "signup", tenant="acme-inc", email="Ana@Acme.com", company="Acme Inc")
    assert out == {"tenant": "acme-inc", "email": "ana@acme.com", "status": "signed up", "email_sent": True}
    st = call(adm, "status", tenant="acme-inc")
    assert st["status"] == "active" and st["plan"] == "free" and st["daily_cap_bytes"] == 10**9
    assert st["keys"] == [] and st["company"] == "Acme Inc" and st["signed_up_by"] == "ana@acme.com"
    assert streams() == ["obs-t-acme-inc-logs", "obs-t-acme-inc-metrics", "obs-t-acme-inc-traces"]
    assert call(adm, "users", tenant="acme-inc")["users"][0]["role"] == "owner"
    assert login("ana@acme.com")["custom:tenant"] == "acme-inc"
    (m,) = [message(x) for x in mail]
    assert m["to"] == ["ana@acme.com"] and m["subject"] == "Your Leasyd account is ready"
    password = __import__("re").search(r"Temporary password</td><td[^>]*>([^<]+)<", m["text"]).group(1)   # moto keeps the HTML
    assert len(password) == 14 and "Acme Inc" in m["text"] and "https://app.leasyd.com" in m["text"]
    # only a reserved name can be signed up
    assert "not reserved" in call(adm, "signup", tenant="other", email="b@b.com", company="B")["error"]


def test_invitations_are_sent_with_ses_and_name_the_inviter(adm, mail):
    call(adm, "create", tenant="acme", company="Acme")
    out = call(adm, "invite-user", tenant="acme", email="bo@acme.com", role="member", invited_by="ana@acme.com")
    assert out["role"] == "member" and out["email_sent"] is True
    (m,) = [message(x) for x in mail]
    assert m["subject"] == "You're invited to Acme on Leasyd" and "ana@acme.com invited you" in m["text"]
    assert "Temporary password</td>" in m["text"]
    assert "unknown role" in call(adm, "invite-user", tenant="acme", email="c@acme.com", role="admin")["error"]
    call(adm, "invite-user", tenant="acme", email="d@acme.com", send_email=False)
    assert len(mail) == 1                                      # send_email=False: nothing sent


def test_more_keys_and_daily_caps(adm):
    call(adm, "create", tenant="acme")
    first = call(adm, "status", tenant="acme")["keys"][0]["api_key_id"]
    out = call(adm, "add-key", tenant="acme", scope="read")
    assert out["scope"] == "read" and out["api_key"].startswith("obs_")
    assert key_status(adm, "acme") == {first: "active", out["key_id"]: "active"}     # others unchanged
    assert "daily_cap_bytes" not in call(adm, "status", tenant="acme")                  # standard: no cap
    assert call(adm, "set-cap", tenant="acme", daily_gb=2.5)["daily_cap_bytes"] == 2_500_000_000
    assert call(adm, "status", tenant="acme")["daily_cap_bytes"] == 2_500_000_000
    assert call(adm, "set-cap", tenant="acme", daily_gb=0)["daily_cap_bytes"] is None
    assert "daily_cap_bytes" not in call(adm, "status", tenant="acme")
    assert "number of GB" in call(adm, "set-cap", tenant="acme", daily_gb="lots")["error"]
    boto3.resource("dynamodb").Table("obs-tenants").put_item(
        Item={"pk": f"meter#acme#{adm._now():%Y-%m-%d}", "tenant": "acme", "bytes": 1234, "records": 5})
    assert call(adm, "status", tenant="acme")["today"] == {"bytes": 1234, "records": 5, "refused_bytes": 0}
    # The daily search allowance: the plan's, a tenant's own, or none.
    assert call(adm, "status", tenant="acme")["searches"] == {"units_today": 0, "units_per_day": 20_000}
    boto3.resource("dynamodb").Table("obs-tenants").put_item(Item={"pk": f"usage#search#acme#{adm._now():%Y-%m-%d}", "n": 42})
    assert call(adm, "set-search", tenant="acme", units_per_day=50_000)["searches"] == {"units_today": 42, "units_per_day": 50_000}
    assert call(adm, "set-search", tenant="acme", units_per_day=0)["searches"]["units_per_day"] is None
    assert call(adm, "set-search", tenant="acme")["searches"]["units_per_day"] == 20_000
    assert "whole number" in call(adm, "set-search", tenant="acme", units_per_day=-1)["error"]
