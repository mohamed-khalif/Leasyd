"""Worker crash/re-run behaviour, against moto's in-memory S3 and DynamoDB.
The real-AWS version of this test is infra/phase2-test.sh."""

import gzip
import json
import os
import types

import boto3
import duckdb
import pytest
from moto import mock_aws

os.environ.update(
    BUCKET="obs-data-test", INDEX_TABLE="obs-index", WORKER_FUNCTION="obs-compaction-worker",
    ALLOW_CRASH_INJECTION="true", AWS_DEFAULT_REGION="us-east-1",
    TENANT_READER_ROLE_ARN="arn:aws:iam::123456789012:role/obs-tenant-reader",
    AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
)
os.environ.pop("AWS_SESSION_TOKEN", None)

H20 = 1790452800 * 10**9


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        import importlib
        import handler
        importlib.reload(handler)
        boto3.client("s3").create_bucket(Bucket="obs-data-test")
        boto3.client("dynamodb").create_table(
            TableName="obs-index", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
        )
        invoked = []
        monkeypatch.setattr(handler, "_invoke_worker", invoked.append)
        yield handler, invoked


def put_raw(n_files, per_file, service="api", tenant="acme"):
    s3 = boto3.client("s3")
    for f in range(n_files):
        recs = [{"timeUnixNano": str(H20 + (f * per_file + r) * 10**6), "body": {"stringValue": "m"}}
                for r in range(per_file)]
        doc = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": service}}]},
            "scopeLogs": [{"scope": {}, "logRecords": recs}]}]}
        s3.put_object(Bucket="obs-data-test",
                      Key=f"_incoming/tenant={tenant}/logs/dt=2026-09-26/hour=20/logs_{f:04d}.json.gz",
                      Body=gzip.compress(json.dumps(doc).encode()))


def state(tmp_path):
    s3 = boto3.client("s3")
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="obs-data-test").get("Contents", [])]
    incoming = [k for k in keys if k.startswith("_incoming/")]
    parquet = [k for k in keys if k.endswith(".parquet")]
    rows = 0
    for k in parquet:
        p = tmp_path / k.replace("/", "_")
        s3.download_file("obs-data-test", k, str(p))
        rows += duckdb.sql(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0]
    items = boto3.client("dynamodb").scan(TableName="obs-index")["Items"]
    index = [i for i in items if not i["pk"]["S"].startswith("_") and "#_services#" not in i["pk"]["S"]]
    internal = [i for i in items if i["pk"]["S"].startswith(("_plan#", "_lease#"))]
    return incoming, parquet, rows, index, internal


HOUR = {"tenant": "acme", "signal": "logs", "dt": "2026-09-26", "hour": "20"}


def ctx(n=1):
    return types.SimpleNamespace(aws_request_id=f"req-{n}")


def plan(handler):
    return handler.dispatcher({"plan_only": HOUR}, ctx(0))["planned"]


def test_clean_run(aws, tmp_path):
    handler, _ = aws
    put_raw(4, 250)
    [b] = plan(handler)
    out = handler.worker({**HOUR, "batch_id": b}, ctx())
    assert out["inputs"] == 4 and not out["resumed"]
    incoming, parquet, rows, index, internal = state(tmp_path)
    assert incoming == [] and rows == 1000 and len(parquet) == 1 and len(index) == 1 and internal == []
    assert index[0]["row_count"]["N"] == "1000"
    assert parquet[0] == f"data/tenant=acme/logs/dt=2026-09-26/hour=20/service=api/part-{b}-000.parquet"
    assert index[0]["pk"]["S"] == "acme#logs#api"


@pytest.mark.parametrize("step", ["write", "index", "commit", "partial_delete"])
def test_crash_then_rerun_loses_and_duplicates_nothing(aws, tmp_path, step):
    handler, _ = aws
    put_raw(4, 250)
    [b] = plan(handler)
    with pytest.raises(RuntimeError, match="injected crash"):
        handler.worker({**HOUR, "batch_id": b, "crash_after": step}, ctx(1))
    # The next dispatcher run re-plans nothing new and returns the same plan.
    assert plan(handler) == [b]
    out = handler.worker({**HOUR, "batch_id": b}, ctx(2))
    assert out["resumed"] == (step in ("commit", "partial_delete"))
    incoming, parquet, rows, index, internal = state(tmp_path)
    assert incoming == []
    assert rows == 1000, "rows lost or duplicated"
    assert len(parquet) == 1 and len(index) == 1
    assert internal == [], "plan or lease left behind"


def test_lease_blocks_concurrent_worker(aws, tmp_path):
    handler, _ = aws
    put_raw(1, 10)
    [b] = plan(handler)
    lease = handler._acquire_lease(f"_lease#acme#logs#2026-09-26#20#{b}", ctx(1), 60)
    assert lease is not None
    assert "skipped" in handler.worker({**HOUR, "batch_id": b}, ctx(2))
    handler._release_lease(*lease)
    assert handler.worker({**HOUR, "batch_id": b}, ctx(3))["inputs"] == 1


def test_busy_hour_split_into_parallel_chunks(aws, tmp_path, monkeypatch):
    handler, _ = aws
    monkeypatch.setattr(handler, "MAX_INPUT_FILES", 2)
    put_raw(5, 10)
    batches = plan(handler)
    assert len(batches) == 3  # 2 + 2 + 1 files
    # Each chunk has its own lease, so all three can hold one at once.
    leases = [handler._acquire_lease(f"_lease#acme#logs#2026-09-26#20#{b}", ctx(i), 60) for i, b in enumerate(batches)]
    assert all(leases)
    for l in leases:
        handler._release_lease(*l)
    for i, b in enumerate(batches):
        handler.worker({**HOUR, "batch_id": b}, ctx(10 + i))
    incoming, parquet, rows, index, internal = state(tmp_path)
    assert incoming == [] and rows == 50 and len(parquet) == 3 and len(index) == 3 and internal == []


def test_late_file_gets_its_own_plan(aws, tmp_path):
    handler, _ = aws
    put_raw(2, 10)
    [b1] = plan(handler)
    boto3.client("s3").put_object(Bucket="obs-data-test", Key="_incoming/tenant=acme/logs/dt=2026-09-26/hour=20/logs_zzzz.json.gz",
                                  Body=gzip.compress(json.dumps({"resourceLogs": []}).encode()))
    b = plan(handler)
    assert b[0] == b1 and len(b) == 2, "existing plan kept, late file planned separately"


def test_large_output_split_into_parts(aws, tmp_path, monkeypatch):
    handler, _ = aws
    monkeypatch.setattr(handler, "MAX_ROWS_PER_FILE", 300)
    put_raw(4, 250)
    [b] = plan(handler)
    handler.worker({**HOUR, "batch_id": b}, ctx())
    incoming, parquet, rows, index, internal = state(tmp_path)
    assert rows == 1000 and len(parquet) == 4 and len(index) == 4
    assert sorted(i["row_count"]["N"] for i in index) == ["100", "300", "300", "300"]


def test_dispatcher_plans_and_invokes_only_closed_hours(aws, monkeypatch):
    handler, invoked = aws
    from datetime import datetime, timezone
    s3 = boto3.client("s3")
    for tenant, dt, hour in [("acme", "2026-09-26", "20"), ("acme", "2026-09-26", "21"), ("globex", "2026-09-26", "20")]:
        s3.put_object(Bucket="obs-data-test", Key=f"_incoming/tenant={tenant}/logs/dt={dt}/hour={hour}/x.json.gz",
                      Body=b"")
    s3.put_object(Bucket="obs-data-test", Key="_incoming/logs/dt=2026-09-26/hour=20/old-layout.json.gz", Body=b"")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 26, 21, 15, tzinfo=timezone.utc)  # 20:00 hour closed + 15 min

    monkeypatch.setattr(handler, "datetime", Clock)
    out = handler.dispatcher({}, ctx())
    assert sorted((e["tenant"], e["dt"], e["hour"]) for e in invoked) == [
        ("acme", "2026-09-26", "20"), ("globex", "2026-09-26", "20")]
    assert [e["batch_id"] for e in invoked] == out["planned"]
    # A second run re-invokes the same plan (retry) instead of planning again.
    handler.dispatcher({}, ctx(2))
    assert [e["batch_id"] for e in invoked] == out["planned"] * 2


def test_dispatcher_lease_blocks_overlapping_runs(aws):
    handler, invoked = aws
    put_raw(1, 10)
    lease = handler._acquire_lease("_lease#dispatcher#logs", ctx(1), 60)
    out = handler.dispatcher({"plan_only": HOUR}, ctx(2))
    assert out["skipped"] == ["logs"] and out["planned"] == []
    handler._release_lease(*lease)


def test_tenants_compacted_separately(aws, tmp_path):
    handler, _ = aws
    put_raw(2, 10, tenant="acme")
    put_raw(3, 10, tenant="globex")
    for tenant in ("acme", "globex"):
        ev = {**HOUR, "tenant": tenant}
        for b in handler.dispatcher({"plan_only": ev}, ctx(0))["planned"]:
            handler.worker({**ev, "batch_id": b}, ctx(1))
    incoming, parquet, rows, index, internal = state(tmp_path)
    assert incoming == [] and rows == 50 and internal == []
    assert sorted(k.split("/")[1] for k in parquet) == ["tenant=acme", "tenant=globex"]
    assert sorted(i["pk"]["S"] for i in index) == ["acme#logs#api", "globex#logs#api"]
    by_pk = {i["pk"]["S"]: i["row_count"]["N"] for i in index}
    assert by_pk == {"acme#logs#api": "20", "globex#logs#api": "30"}


def test_invalid_tenant_rejected(aws):
    handler, _ = aws
    with pytest.raises(ValueError):
        handler.worker({**HOUR, "tenant": "../other", "batch_id": "x"}, ctx())
    with pytest.raises(ValueError):
        handler.dispatcher({"plan_only": {**HOUR, "tenant": "A#B"}}, ctx())
