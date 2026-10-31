"""Deploys: what CI may post, and how versions seen in the telemetry become changes."""
from datetime import datetime, timezone

import pytest

from domain.deploys import parse, version_changes

NOW = datetime(2026, 10, 30, 2, 40, tzinfo=timezone.utc)


def test_a_deploy_needs_a_service_and_a_version_and_gets_a_stable_id():
    d = parse({"service": "payment", "version": "3.1.0", "environment": "production", "commit": "9f2c1e7",
               "url": "https://github.com/shop/payment/actions/runs/811", "deployed_at": "2026-10-30T02:11:30Z"}, NOW)
    assert (d.service, d.version, d.deployed_at, d.status) == ("payment", "3.1.0", "2026-10-30T02:11:30Z", "succeeded")
    assert d.id == parse({"service": "payment", "version": "3.1.0", "environment": "production", "deployed_at": "2026-10-30T02:11:30Z"}, NOW).id
    assert parse({"service": "payment", "version": 42}, NOW).deployed_at == "2026-10-30T02:40:00Z"       # now, by default
    assert parse({"service": "a", "version": "1", "id": "gh-811", "status": "rolled_back"}, NOW).id == "gh-811"


@pytest.mark.parametrize("body, why", [
    ({"version": "1"}, "service is required"), ({"service": "a"}, "version is required"),
    ({"service": "a", "version": "1", "url": "javascript:alert(1)"}, "url"),
    ({"service": "a", "version": "1", "status": "maybe"}, "status"),
    ({"service": "a", "version": "1", "deployed_at": "2026-10-01T00:00:00Z"}, "last 7 days"),
    ({"service": "a", "version": "1", "deployed_at": "2026-10-30T03:40:00Z"}, "last 7 days"),
    ({"service": "a", "version": "1", "deployed_at": "yesterday"}, "ISO-8601"),
    ({"service": "a" * 129, "version": "1"}, "at most 128"), ({"service": {"x": 1}, "version": "1"}, "service: text"),
    ({"service": "a", "version": "1", "id": "a b"}, "id"), ([], "JSON object")])
def test_bad_deploys_are_refused_with_the_reason(body, why):
    with pytest.raises(ValueError, match=why):
        parse(body, NOW)


def test_a_version_first_seen_in_the_range_is_a_change_and_the_first_one_is_not():
    rows = [{"service": "cart", "version": "2.4.3", "first_seen": "2026-10-29T01:14:00Z", "last_seen": "2026-10-30T02:12:00Z"},
            {"service": "cart", "version": "2.5.0", "first_seen": "2026-10-30T02:12:00Z", "last_seen": "2026-10-30T02:40:00Z"},
            {"service": "catalog", "version": "9.0", "first_seen": "2026-10-29T01:14:00Z", "last_seen": "2026-10-30T02:40:00Z"}]
    assert version_changes(rows, "2026-10-29T01:16:00Z") == [
        {"service": "cart", "version": "2.5.0", "first_seen": "2026-10-30T02:12:00Z", "previous": "2.4.3",
         "previous_last_seen": "2026-10-30T02:12:00Z", "source": "inferred from service.version"}]
