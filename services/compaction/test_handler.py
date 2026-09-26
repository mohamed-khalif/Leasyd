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


def put_raw(n_files, per_file, service="api"):
    s3 = boto3.client("s3")
    for f in range(n_files):
        recs = [{"timeUnixNano": str(H20 + (f * per_file + r) * 10**6), "body": {"stringValue": "m"}}
                for r in range(per_file)]
        doc = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": service}}]},
            "scopeLogs": [{"scope": {}, "logRecords": recs}]}]}
        s3.put_object(Bucket="obs-data-test",
                      Key=f"_incoming/logs/dt=2026-09-26/hour=20/logs_{f:04d}.json.gz",
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
    index = [i for i in items if not i["pk"]["S"].startswith("_")]
    internal = [i for i in items if i["pk"]["S"].startswith("_")]
    return incoming, parquet, rows, index, internal


EVENT = {"signal": "logs", "dt": "2026-09-26", "hour": "20"}
CTX = types.SimpleNamespace(aws_request_id="req-1")


def test_clean_run(aws, tmp_path):
    handler, _ = aws
    put_raw(4, 250)
    out = handler.worker(EVENT, CTX)
    assert out["inputs"] == 4
    incoming, parquet, rows, index, internal = state(tmp_path)
    assert incoming == [] and rows == 1000 and len(parquet) == 1 and len(index) == 1 and internal == []
    assert index[0]["row_count"]["N"] == "1000"


@pytest.mark.parametrize("step", ["write", "index", "commit", "partial_delete"])
def test_crash_then_rerun_loses_and_duplicates_nothing(aws, tmp_path, step):
    handler, _ = aws
    put_raw(4, 250)
    with pytest.raises(RuntimeError, match="injected crash"):
        handler.worker({**EVENT, "crash_after": step}, CTX)
    handler.worker(EVENT, types.SimpleNamespace(aws_request_id="req-2"))
    incoming, parquet, rows, index, internal = state(tmp_path)
    assert incoming == []
    assert rows == 1000, "rows lost or duplicated"
    assert len(parquet) == 1 and len(index) == 1
    assert internal == [], "manifest or lease left behind"


def test_lease_blocks_concurrent_worker(aws, tmp_path):
    handler, _ = aws
    put_raw(1, 10)
    lease = handler._acquire_lease("_lease#logs#2026-09-26#20", CTX)
    assert lease is not None
    out = handler.worker(EVENT, types.SimpleNamespace(aws_request_id="req-2"))
    assert "skipped" in out
    handler._release_lease(*lease)
    assert handler.worker(EVENT, types.SimpleNamespace(aws_request_id="req-3"))["inputs"] == 1


def test_large_partition_is_chunked(aws, tmp_path, monkeypatch):
    handler, invoked = aws
    monkeypatch.setattr(handler, "MAX_INPUT_FILES", 3)
    put_raw(5, 10)
    first = handler.worker(EVENT, CTX)
    assert first["inputs"] == 3 and first["remaining"] == 2
    assert invoked == [EVENT]
    second = handler.worker(EVENT, types.SimpleNamespace(aws_request_id="req-2"))
    assert second["inputs"] == 2 and second["remaining"] == 0
    incoming, parquet, rows, index, _ = state(tmp_path)
    assert incoming == [] and rows == 50 and len(parquet) == 2 and len(index) == 2


def test_dispatcher_only_closed_hours(aws, monkeypatch):
    handler, invoked = aws
    from datetime import datetime, timezone
    s3 = boto3.client("s3")
    for dt, hour in [("2026-09-26", "20"), ("2026-09-26", "21")]:
        s3.put_object(Bucket="obs-data-test", Key=f"_incoming/logs/dt={dt}/hour={hour}/x.json.gz", Body=b"")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 26, 21, 15, tzinfo=timezone.utc)  # 20:00 hour closed + 15 min

    monkeypatch.setattr(handler, "datetime", Clock)
    handler.dispatcher({}, None)
    assert invoked == [{"signal": "logs", "dt": "2026-09-26", "hour": "20"}]
