import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))   # limits.py


class _InProcess:
    """obs-query (the coordinator) run in-process and waited for: the API's sync path in tests.
    A test that needs the past-the-deadline behaviour uses test_query.Coordinator instead."""
    def __call__(self, payload, wait_s):
        query = sys.modules["query"]
        if not wait_s:
            raise query._StillRunning()
        return query.handler(payload, None)


@pytest.fixture(autouse=True)
def coordinator_in_process(monkeypatch):
    if "query" in sys.modules:
        query = sys.modules["query"]
        monkeypatch.setattr(query, "_coordinate", _InProcess())
        # Each test starts knowing nothing of any tenant's limits (they live per container).
        query._plans.clear()
        monkeypatch.setattr(query, "_rates", query.limits.LastKnownGood(query.LIMITS_FRESH_S))
        monkeypatch.setattr(query, "_daily", query.limits.LastKnownGood(query.UNITS_FRESH_S))
        monkeypatch.setattr(query, "_local_slots", {})
