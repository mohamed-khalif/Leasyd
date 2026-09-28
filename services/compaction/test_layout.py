import pytest

import layout


@pytest.mark.parametrize("t", ["acme", "acme-corp", "a1b", "x" * 40])
def test_valid_tenants(t):
    assert layout.check_tenant(t) == t


@pytest.mark.parametrize("t", [None, "", "ab", "x" * 41, "Acme", "-acme", "acme-", "ac/me", "ac#me", "ac=me", "ac me"])
def test_invalid_tenants(t):
    with pytest.raises(ValueError):
        layout.check_tenant(t)


def test_everything_a_tenant_reads_is_under_its_prefixes():
    assert layout.data_prefix("acme", "logs").startswith("data/tenant=acme/")
    assert layout.index_pk("acme", "logs", "api").startswith("acme#")
    assert layout.services_pk("acme", "logs").startswith("acme#")
    # Plans and raw-file records are the tenant's own metadata (lookups read
    # them to decide raw vs Parquet visibility); leases are internal.
    assert layout.plan_pk("acme", "logs", "2026-09-26", "20").startswith("acme#_plan#")
    assert layout.raw_files_pk("acme", "logs", "2026-09-26", "20").startswith("acme#_raw#")
    assert layout.worker_lease_pk("acme", "logs", "2026-09-26", "20", "b").startswith("_")


@pytest.mark.parametrize("key,want", [
    ("_incoming/tenant=acme/logs/dt=2026-09-27/hour=10/obs-t-acme-logs-1-2026-09-27-10-00-00-abc.json.gz",
     ("acme", "logs", "2026-09-27", "10")),
    ("_incoming/_errors/tenant=acme/logs/processing-failed/dt=2026-09-27/x.gz", None),
    ("_incoming/tenant=Acme/logs/dt=2026-09-27/hour=10/x", None),
    ("_incoming/tenant=acme/logs/dt=2026-09-27/hour=10/sub/x", None),
    ("data/tenant=acme/logs/dt=2026-09-27/hour=10/service=a/x.parquet", None),
])
def test_parse_incoming_key(key, want):
    assert layout.parse_incoming_key(key) == want
