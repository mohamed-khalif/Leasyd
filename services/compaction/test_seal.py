"""Day filters (T6): sealing, lookups ruling out whole days, and staying
correct when chunks land during or after sealing. Against moto."""

import gzip
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest

from test_fastlane import aws, ctx  # noqa: F401  (fixture)

H10 = 1790416800 * 10**9  # 2026-09-26T10:00:00Z
DAY = "2026-09-26"
RANGE = dict(tenant="acme", start="2026-09-26T00:00:00Z", end="2026-09-26T23:59:59Z")


def put(arrival_hour, name, traces, service="api"):
    recs = [{"timeUnixNano": str(H10 + i * 10**9), "body": {"stringValue": "m"}, "traceId": t}
            for i, t in enumerate(traces)]
    doc = {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": service}}]}, "scopeLogs": [{"logRecords": recs}]}]}
    key = f"_incoming/tenant=acme/logs/dt={DAY}/hour={arrival_hour}/{name}.json.gz"
    boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=gzip.compress(json.dumps(doc).encode()))


def compact_hour(handler, arrival_hour):
    hour = {"tenant": "acme", "signal": "logs", "dt": DAY, "hour": arrival_hour}
    for b in handler.dispatcher({"plan_only": hour}, ctx(0))["planned"]:
        handler.worker({**hour, "batch_id": b}, ctx(1))


def day_item():
    return boto3.client("dynamodb").get_item(TableName="obs-index", Key={
        "pk": {"S": "acme#_day#logs"}, "sk": {"S": DAY}}).get("Item", {})


def seal(handler):
    return handler.sealer({"only": {"tenant": "acme", "signal": "logs", "dt": DAY}}, None)


def keys(prefix):
    return [o["Key"] for o in boto3.client("s3").list_objects_v2(
        Bucket="obs-data-test", Prefix=prefix).get("Contents", [])]


T = [f"{i:032x}" for i in range(200)]


@pytest.fixture
def sealed(aws, monkeypatch):  # noqa: F811
    handler, lookup = aws
    monkeypatch.setattr(handler, "BLOOM_INLINE_MAX_BYTES", 0)  # per-file blooms in S3, to count fetches
    put("10", "a", T[:100], "api")
    put("10", "b", T[100:], "web")
    compact_hour(handler, "10")
    assert day_item()["dirty"]["N"] == "1"
    assert len(keys("data/tenant=acme/logs/_ids/")) > 0
    assert seal(handler)["sealed"] == [f"acme/logs/{DAY}"]
    return handler, lookup


def test_sealed_day_rules_out_unknown_ids_without_reading_file_blooms(sealed):
    handler, lookup = sealed
    item = day_item()
    assert item["sealed"]["N"] == item["dirty"]["N"] == "1" and item["ids"]["N"] == "200"
    assert len(keys(f"data/tenant=acme/logs/_bloom/day/dt={DAY}/v=1/")) == 16
    hit = lookup.lookup(**RANGE, match={"trace_id": T[150]})
    assert [f["service"] for f in hit["files"]] == ["web"] and hit["stats"]["days_checked"] == 1
    miss = lookup.lookup(**RANGE, match={"trace_id": "f" * 32})
    assert miss["files"] == [] and miss["stats"]["days_ruled_out"] == 1 and miss["stats"]["bloom_fetches"] == 0
    # A field the day filter doesn't cover: no day pruning, per-file blooms decide.
    other = lookup.lookup(**RANGE, match={"span_id": "x"})
    assert other["stats"]["days_checked"] == 0


def test_late_chunk_makes_the_day_untrusted_until_resealed(sealed):
    handler, lookup = sealed
    late = "a" * 32
    put("23", "late", [late])
    compact_hour(handler, "23")
    assert day_item()["dirty"]["N"] == "2" and day_item()["sealed"]["N"] == "1"
    out = lookup.lookup(**RANGE, match={"trace_id": late})
    assert len(out["files"]) == 1 and out["stats"]["days_checked"] == 0   # fell back to per-file blooms
    assert seal(handler)["sealed"] == [f"acme/logs/{DAY}"]
    out = lookup.lookup(**RANGE, match={"trace_id": late})
    assert len(out["files"]) == 1 and out["stats"]["days_checked"] == 1
    assert keys(f"data/tenant=acme/logs/_bloom/day/dt={DAY}/v=1/") == []   # old version removed


def test_chunk_landing_while_sealing_is_not_lost(sealed, monkeypatch):
    handler, lookup = sealed
    put("23", "late", ["b" * 32])
    real_list = handler._list

    def list_then_worker_commits(prefix):
        out = real_list(prefix)
        if "/_ids/" in prefix:  # a worker bumps the day after the sealer listed digests
            boto3.client("dynamodb").update_item(
                TableName="obs-index", Key={"pk": {"S": "acme#_day#logs"}, "sk": {"S": DAY}},
                UpdateExpression="ADD dirty :one", ExpressionAttributeValues={":one": {"N": "1"}})
        return out
    boto3.client("dynamodb").update_item(   # the day is out of date, so the sealer runs
        TableName="obs-index", Key={"pk": {"S": "acme#_day#logs"}, "sk": {"S": DAY}},
        UpdateExpression="ADD dirty :one", ExpressionAttributeValues={":one": {"N": "1"}})
    monkeypatch.setattr(handler, "_list", list_then_worker_commits)
    assert seal(handler)["sealed"] == []
    item = day_item()
    assert item["sealed"]["N"] != item["dirty"]["N"]   # not trusted: lookups use per-file blooms


def test_digests_deleted_after_keep_window_and_day_then_stays_on_file_blooms(sealed, monkeypatch):
    handler, lookup = sealed
    later = datetime(2026, 9, 26, tzinfo=timezone.utc) + timedelta(days=5)
    assert handler._seal_day("acme", "logs", DAY, later, None) == "ids_deleted"
    assert keys("data/tenant=acme/logs/_ids/") == []
    assert lookup.lookup(**RANGE, match={"trace_id": "f" * 32})["stats"]["days_ruled_out"] == 1  # still sealed
    late = "c" * 32
    put("23", "late", [late])
    compact_hour(handler, "23")
    assert handler._seal_day("acme", "logs", DAY, later, None) == "nothing to seal"  # can't re-seal without digests
    assert len(lookup.lookup(**RANGE, match={"trace_id": late})["files"]) == 1


def test_scheduled_sealer_waits_for_the_day_to_close(sealed, monkeypatch):
    handler, _ = sealed
    put("23", "late", ["d" * 32])
    compact_hour(handler, "23")
    assert handler.sealer({}, None)["sealed"] == [f"acme/logs/{DAY}"]   # 2026-09-26 closed long ago


def test_blooms_of_one_chunk_spanning_hours_do_not_collide(aws, monkeypatch):  # noqa: F811
    """One chunk, one service, two event hours: each output file keeps its own bloom
    (they used to share an S3 key, so one overwrote the other)."""
    handler, lookup = aws
    monkeypatch.setattr(handler, "BLOOM_INLINE_MAX_BYTES", 0)
    early = [f"{i:032x}" for i in range(50)]
    late = [f"{i:032x}" for i in range(1000, 1300)]           # a different size of bloom
    recs = ([{"timeUnixNano": str(H10 + i), "body": {"stringValue": "m"}, "traceId": t} for i, t in enumerate(early)]
            + [{"timeUnixNano": str(H10 + 3600 * 10**9 + i), "body": {"stringValue": "m"}, "traceId": t}
               for i, t in enumerate(late)])
    doc = {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "api"}}]}, "scopeLogs": [{"logRecords": recs}]}]}
    boto3.client("s3").put_object(Bucket="obs-data-test", Key=f"_incoming/tenant=acme/logs/dt={DAY}/hour=11/x.json.gz",
                                  Body=gzip.compress(json.dumps(doc).encode()))
    compact_hour(handler, "11")
    blooms = [k for k in keys("data/tenant=acme/logs/_bloom/") if "/day/" not in k]
    assert len(blooms) == 2
    for t in (early[7], late[7]):
        assert len(lookup.lookup(**RANGE, match={"trace_id": t})["files"]) == 1
