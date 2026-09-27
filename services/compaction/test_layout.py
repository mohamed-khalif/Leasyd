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
    # Internal records never start with a tenant prefix, so no tenant can read them.
    assert layout.plan_pk("acme", "logs", "2026-09-26", "20").startswith("_")
    assert layout.worker_lease_pk("acme", "logs", "2026-09-26", "20", "b").startswith("_")
