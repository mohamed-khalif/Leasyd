"""Load generator and planner, without AWS or network."""

import gzip
import importlib.util
import json
import os
import sys
import time

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
import loadgen  # noqa: E402
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "loadtest", os.path.join(os.path.dirname(__file__), "..", "..", "infra", "t6", "loadtest.py"))
loadtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loadtest)


def test_payloads_are_valid_otlp_batches():
    for signal, build in loadgen.BUILDERS.items():
        msg = build("acme", "svc-01", time.time_ns())
        raw = msg.SerializeToString()
        assert 20_000 < len(raw) < 400_000, signal
        assert type(msg)().FromString(raw) == msg
    logs = loadgen.logs_request("acme", "svc-01", time.time_ns())
    recs = logs.resource_logs[0].scope_logs[0].log_records
    assert len(recs) == loadgen.BATCH_ITEMS and len({r.trace_id for r in recs}) == loadgen.BATCH_ITEMS


def keys(n):
    return {loadtest.tenant_name(r): {"key": f"k{r}", "rank": r, "services": ["a"]} for r in range(n)}


def test_zipf_plan_splits_big_tenants_and_packs_small_ones():
    workers = loadtest.plan_workers(keys(100), gbph=50)
    rate = lambda w: sum(p["bytes_per_s"] for p in w)
    assert all(rate(w) <= loadtest.WORKER_BYTES_PER_S * 1.0001 for w in workers)
    assert all(len(w) <= loadtest.WORKER_MAX_TENANTS for w in workers)
    total = sum(rate(w) for w in workers)
    assert abs(total - 50e9 / 3600) / total < 1e-9                       # every byte planned
    per_tenant = {}
    for w in workers:
        for p in w:
            per_tenant[p["tenant"]] = per_tenant.get(p["tenant"], 0) + p["bytes_per_s"]
    assert len(per_tenant) == 100
    assert per_tenant["t6-000"] / per_tenant["t6-099"] == 100            # Zipf: rank 1 is 100x rank 100
    assert sum(1 for w in workers for p in w if p["tenant"] == "t6-000") > 1  # biggest tenant split


class FakePoster:
    def __init__(self, endpoint, statuses=None):
        self.posts = []

    def post(self, key, signal, body):
        self.posts.append((key, signal, len(body)))
        req = loadgen.BUILDERS[signal]  # noqa: F841
        gzip.decompress(body)
        return (503, 6, 0.1) if key == "bad" else (200, 1, 0.05)


class FakeDdb:
    def __init__(self):
        self.items = []

    def put_item(self, TableName, Item):
        self.items.append(Item)


def test_send_paces_and_records(monkeypatch):
    fake_ddb = FakeDdb()
    posters = []
    monkeypatch.setattr(loadgen, "Poster", lambda ep: posters.append(FakePoster(ep)) or posters[-1])
    monkeypatch.setattr(loadgen, "ddb", fake_ddb)
    ev = {"run": "t6", "step": "x", "worker": "0-0", "endpoint": "https://example.com/ingest", "duration_s": 2,
          "tenants": [{"tenant": "a", "key": "good", "bytes_per_s": 400_000, "services": ["s"]},
                      {"tenant": "b", "key": "bad", "bytes_per_s": 100_000, "services": ["s"]}]}
    out = loadgen.send(ev, None)
    [item] = fake_ddb.items
    assert item["pk"]["S"] == "t6#x" and item["sk"]["S"].startswith("send#0-0#")
    st = json.loads(out["status"])
    assert st.get("200", 0) >= 2 and st.get("503", 0) >= 1
    assert int(out["dropped"]) == st["503"] and int(out["retries"]) == 5 * st["503"]
    # Paced: roughly the target volume in 2 s, not as fast as possible.
    assert int(out["bytes"]) < 400_000 * 2 * 3
