"""Fast lane (T3): raw files are searchable on arrival, and the handover to
compacted Parquet never shows a record twice or loses it. Against moto.
The real-AWS version is infra/phaseT3-test.sh."""

import gzip
import json
import types
import urllib.parse

import boto3
import pytest
from moto import mock_aws

import test_handler  # noqa: F401  (sets the environment)

H10 = 1790416800 * 10**9  # 2026-09-26T10:00:00Z
RAW = "_incoming/tenant=acme/logs/dt=2026-09-26/hour=10/obs-t-acme-logs-1-2026-09-26-10-05-00-abc.json.gz"
HOUR = {"tenant": "acme", "signal": "logs", "dt": "2026-09-26", "hour": "10"}
RANGE = dict(tenant="acme", start="2026-09-26T09:00:00Z", end="2026-09-26T11:59:59Z")


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        import importlib
        import handler
        import lookup
        importlib.reload(handler)
        importlib.reload(lookup)
        boto3.client("s3").create_bucket(Bucket="obs-data-test")
        boto3.client("dynamodb").create_table(
            TableName="obs-index", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
        )
        monkeypatch.setattr(handler, "_invoke_worker", lambda p: None)
        yield handler, lookup


def put_raw(key=RAW, services=("api", "web"), n=50):
    """A Firehose-style object: several OTLP JSON lines, several services."""
    lines = []
    for s_i, svc in enumerate(services):
        recs = [{"timeUnixNano": str(H10 + (s_i * 1000 + i) * 10**9), "body": {"stringValue": "m"},
                 "traceId": f"{svc}-{i}".encode().hex().ljust(32, "0")[:32]} for i in range(n)]
        lines.append(json.dumps({"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": svc}}]}, "scopeLogs": [{"logRecords": recs}]}]}))
    boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=gzip.compress("\n".join(lines).encode()))


def event(key=RAW, encode=True):
    k = urllib.parse.quote_plus(key, safe="/") if encode else key
    return {"detail-type": "Object Created", "detail": {"bucket": {"name": "obs-data-test"}, "object": {"key": k}}}


def ctx(n=1):
    return types.SimpleNamespace(aws_request_id=f"req-{n}")


def visible(lookup, **kw):
    out = lookup.lookup(**{**RANGE, **kw})
    return out, sum(f["row_count"] for f in out["files"]), sorted({f["kind"] for f in out["files"]})


def test_raw_file_searchable_on_arrival(aws):
    handler, lookup = aws
    put_raw()
    res = handler.recent_indexer(event(), None)
    assert res["entries"] == 2 and res["rows"] == 100
    out, rows, kinds = visible(lookup)
    assert rows == 100 and kinds == ["raw"]
    assert {f["service"] for f in out["files"]} == {"api", "web"}
    assert all(f["file_path"] == f"s3://obs-data-test/{RAW}" for f in out["files"])
    # bloom filters work on raw entries too
    out, _, _ = visible(lookup, match={"trace_id": "web-7".encode().hex().ljust(32, "0")[:32]})
    assert [f["service"] for f in out["files"]] == ["web"]


def test_indexer_is_idempotent(aws):
    handler, lookup = aws
    put_raw()
    handler.recent_indexer(event(), None)
    handler.recent_indexer(event(), None)  # EventBridge may deliver twice
    assert visible(lookup)[1] == 100


@pytest.mark.parametrize("crash_after,want_kinds", [
    (None, ["parquet"]),            # clean run
    ("write", ["raw"]),             # Parquet files exist but aren't indexed
    ("index", ["raw"]),             # Parquet indexed, plan still "planned": hidden
    ("commit", ["parquet"]),        # committed: Parquet visible, raw hidden (not yet retired)
    ("retire", ["parquet"]),        # raw entries gone, raw files still there
    ("partial_delete", ["parquet"]),
])
def test_handover_never_double_counts_or_loses(aws, crash_after, want_kinds):
    handler, lookup = aws
    put_raw()
    handler.recent_indexer(event(), None)
    assert visible(lookup)[1] == 100
    [b] = handler.dispatcher({"plan_only": HOUR}, ctx(0))["planned"]
    assert visible(lookup)[1] == 100  # planned, nothing written yet
    if crash_after:
        with pytest.raises(RuntimeError, match="injected crash"):
            handler.worker({**HOUR, "batch_id": b, "crash_after": crash_after}, ctx(1))
        out, rows, kinds = visible(lookup)
        assert rows == 100, f"after crash at {crash_after}: {rows} rows visible"
        assert kinds == want_kinds
        handler.worker({**HOUR, "batch_id": b}, ctx(2))  # the retry
    else:
        handler.worker({**HOUR, "batch_id": b}, ctx(1))
    out, rows, kinds = visible(lookup)
    assert rows == 100 and kinds == ["parquet"]
    items = boto3.client("dynamodb").scan(TableName="obs-index")["Items"]
    assert not [i for i in items if "#_raw#" in i["pk"]["S"] or "#_plan#" in i["pk"]["S"]
                or i.get("kind", {}).get("S") == "raw"], "fast-lane leftovers"


def test_file_arriving_after_planning_stays_raw_until_its_own_plan(aws):
    handler, lookup = aws
    put_raw()
    handler.recent_indexer(event(), None)
    [b] = handler.dispatcher({"plan_only": HOUR}, ctx(0))["planned"]
    late = RAW.replace("abc", "late")
    put_raw(key=late, services=("api",), n=10)
    handler.recent_indexer(event(late), None)
    handler.worker({**HOUR, "batch_id": b}, ctx(1))
    out, rows, kinds = visible(lookup)
    assert rows == 110 and kinds == ["parquet", "raw"]  # the late file is still raw
    b2 = [x for x in handler.dispatcher({"plan_only": HOUR}, ctx(2))["planned"] if x != b]
    handler.worker({**HOUR, "batch_id": b2[0]}, ctx(3))
    assert visible(lookup)[1:] == (110, ["parquet"])


def test_indexer_skips_non_raw_and_missing_objects(aws):
    handler, lookup = aws
    assert "skipped" in handler.recent_indexer(event("_incoming/_errors/tenant=acme/logs/x/dt=2026-09-26/f.gz"), None)
    assert "skipped" in handler.recent_indexer(event(RAW.replace("/logs/", "/traces/")), None)
    assert "skipped" in handler.recent_indexer(event(RAW), None)  # never written / already compacted
    assert visible(lookup)[1] == 0


def test_unencoded_key_also_works(aws):
    handler, lookup = aws
    put_raw()
    handler.recent_indexer(event(encode=False), None)
    assert visible(lookup)[1] == 100
