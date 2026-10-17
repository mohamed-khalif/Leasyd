import sys

import pytest


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
        monkeypatch.setattr(sys.modules["query"], "_coordinate", _InProcess())
