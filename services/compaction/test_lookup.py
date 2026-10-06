"""Index lookup against moto, on files produced by the real worker.
The real-AWS version of this test is infra/phase3-test.sh."""

import gzip
import json
import types

import boto3
import pytest
from moto import mock_aws

import test_handler  # sets the environment and provides the fixture pieces

H10 = 1790416800 * 10**9  # 2026-09-26T10:00:00Z in ns


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
        test_handler.create_usage_table()
        monkeypatch.setattr(handler, "_invoke_worker", lambda p: None)
        yield handler, lookup


def seed_and_compact(handler, service, hour, n=200, tenant="acme"):
    """One arrival hour of logs for a service, each with its own trace and request id."""
    start = H10 + hour * 3600 * 10**9
    recs = [{"timeUnixNano": str(start + i * (3500 * 10**9 // n)), "body": {"stringValue": "m"},
             "traceId": f"{service}-{hour:02d}-{i:04d}".encode().hex()[:32].ljust(32, "0"),
             "attributes": [{"key": "request.id", "value": {"stringValue": f"req-{service}-{hour}-{i}"}}]}
            for i in range(n)]
    doc = {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": service}}]},
        "scopeLogs": [{"scope": {}, "logRecords": recs}]}]}
    hh = f"{10 + hour:02d}"
    boto3.client("s3").put_object(
        Bucket="obs-data-test", Key=f"_incoming/tenant={tenant}/logs/dt=2026-09-26/hour={hh}/logs_{service}.json.gz",
        Body=gzip.compress(json.dumps(doc).encode()))
    ev = {"tenant": tenant, "signal": "logs", "dt": "2026-09-26", "hour": hh}
    for b in handler.dispatcher({"plan_only": ev}, types.SimpleNamespace(aws_request_id="d"))["planned"]:
        handler.worker({**ev, "batch_id": b}, types.SimpleNamespace(aws_request_id=b))
    return recs


def trace_of(recs, i):
    return recs[i]["traceId"]


def test_time_range_prunes_to_overlapping_files(aws):
    handler, lookup = aws
    for svc in ("api", "web"):
        for h in range(3):  # 10:00, 11:00, 12:00
            seed_and_compact(handler, svc, h)
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:30:00Z", end="2026-09-26T11:15:00Z", services=["api"])
    assert [f["min_ts"][:13] for f in out["files"]] == ["2026-09-26T10", "2026-09-26T11"]
    assert all(f["service"] == "api" for f in out["files"])


def test_file_starting_before_range_is_included(aws):
    handler, lookup = aws
    seed_and_compact(handler, "api", 0)  # one file 10:00-10:58
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:45:00Z", end="2026-09-26T10:46:00Z")
    assert len(out["files"]) == 1


def test_file_ending_before_range_is_excluded(aws):
    handler, lookup = aws
    seed_and_compact(handler, "api", 0)
    out = lookup.lookup(tenant="acme", start="2026-09-26T11:00:00Z", end="2026-09-26T12:00:00Z")
    assert out["files"] == [] and out["stats"]["in_time_range"] == 0


def test_no_services_given_searches_all(aws):
    handler, lookup = aws
    seed_and_compact(handler, "api", 0)
    seed_and_compact(handler, "web", 0)
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T10:59:00Z")
    assert sorted(f["service"] for f in out["files"]) == ["api", "web"]
    assert out["stats"]["services"] == 2


@pytest.mark.parametrize("inline", [True, False])
def test_trace_lookup_opens_only_the_file_that_has_it(aws, monkeypatch, inline):
    handler, lookup = aws
    if not inline:
        monkeypatch.setattr(handler, "BLOOM_INLINE_MAX_BYTES", 0)
    seeded = {(svc, h): seed_and_compact(handler, svc, h) for svc in ("api", "web", "db") for h in range(3)}
    target = trace_of(seeded[("web", 1)], 17)
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T12:59:00Z", match={"trace_id": target})
    assert out["stats"]["in_time_range"] == 9
    assert [(f["service"], f["min_ts"][:13]) for f in out["files"]][:1] == [("web", "2026-09-26T11")]
    assert len(out["files"]) <= 2  # the right file, plus at most a rare false positive


def test_request_id_lookup(aws):
    handler, lookup = aws
    for h in range(3):
        seed_and_compact(handler, "api", h)
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T12:59:00Z",
                        match={"request.id": "REQ-api-2-5"})
    assert out["files"] and out["files"][0]["min_ts"].startswith("2026-09-26T12")
    assert len(out["files"]) <= 2


def test_unknown_id_prunes_everything(aws):
    handler, lookup = aws
    for h in range(3):
        seed_and_compact(handler, "api", h)
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T12:59:00Z",
                        match={"trace_id": "f" * 32})
    assert len(out["files"]) <= 1


def test_files_without_blooms_are_never_pruned(aws):
    handler, lookup = aws
    seed_and_compact(handler, "api", 0)
    ddb = boto3.client("dynamodb")
    for item in ddb.scan(TableName="obs-index")["Items"]:
        if item["pk"]["S"] == "acme#logs#api":
            for k in ("bloom", "bloom_fields", "bloom_m", "bloom_k", "bloom_n"):
                item.pop(k, None)
            ddb.put_item(TableName="obs-index", Item=item)  # like an entry from before Phase 3
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T10:59:00Z",
                        match={"trace_id": "f" * 32})
    assert len(out["files"]) == 1


def test_tenant_sees_only_its_own_files(aws):
    handler, lookup = aws
    acme = seed_and_compact(handler, "api", 0, tenant="acme")
    seed_and_compact(handler, "api", 0, tenant="globex")
    seed_and_compact(handler, "billing", 0, tenant="globex")
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T10:59:00Z")
    assert [f["file_path"].split("/")[4] for f in out["files"]] == ["tenant=acme"]
    assert out["stats"]["services"] == 1  # globex's "billing" isn't listed for acme
    # Even naming another tenant's service or its trace id finds nothing outside acme.
    out = lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T10:59:00Z",
                        services=["billing"])
    assert out["files"] == []
    out = lookup.lookup(tenant="globex", start="2026-09-26T10:00:00Z", end="2026-09-26T10:59:00Z",
                        match={"trace_id": trace_of(acme, 3)})
    assert len(out["files"]) <= 1 and all("tenant=globex" in f["file_path"] for f in out["files"])


def test_lookup_requires_valid_tenant(aws):
    _, lookup = aws
    for bad in (None, "", "Acme", "a", "acme#x", "../acme"):
        with pytest.raises(ValueError):
            lookup.lookup(tenant=bad, start="2026-09-26T10:00:00Z", end="2026-09-26T11:00:00Z")


def test_lookup_uses_tenant_tagged_session(aws, monkeypatch):
    _, lookup = aws
    calls = []
    real = lookup.sts.assume_role
    monkeypatch.setattr(lookup.sts, "assume_role", lambda **kw: calls.append(kw) or real(**kw))
    lookup._sessions.clear()
    lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T11:00:00Z")
    lookup.lookup(tenant="acme", start="2026-09-26T10:00:00Z", end="2026-09-26T11:00:00Z")
    lookup.lookup(tenant="globex", start="2026-09-26T10:00:00Z", end="2026-09-26T11:00:00Z")
    assert [c["Tags"] for c in calls] == [[{"Key": "tenant", "Value": "acme"}],
                                          [{"Key": "tenant", "Value": "globex"}]]  # cached per tenant


def test_tenant_clients_are_reused_not_rebuilt(aws, monkeypatch):
    # A new client per call kept its connection pool open, and a busy warm container ran out of
    # file descriptors ("Too many open files"): every query on it failed, for every tenant.
    _, lookup = aws
    lookup._sessions.clear()
    first = lookup._clients_for("acme")
    assert lookup._clients_for("acme") == first
    assert lookup._clients_for("globex")[1] is not first[1]
