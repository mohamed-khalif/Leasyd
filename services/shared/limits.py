"""Fail-closed limit checks with a short memory (docs/design-query-lifecycle.md §5).

A limit kept in DynamoDB (requests a minute, units a day, queries running at once) must not
disappear when DynamoDB can't be read: that is exactly when the system is under stress. But a
blip of a few seconds shouldn't stop every customer's queries either. So each check remembers,
per container, the last value it read and what it has counted itself since; while that is fresh
it decides from it, and once it is older than the check's ttl it refuses (the caller answers
503 with Retry-After).

Copied into each Lambda bundle that uses it (infra/build-compaction.sh, infra/build-ai.sh).
"""
import json
import time


class LastKnownGood:
    def __init__(self, ttl_s):
        self.ttl_s = ttl_s
        self._seen = {}     # key -> [value, read at, counted here since]

    def remember(self, key, value):
        """A value just read from the store."""
        self._seen[key] = [value, time.time(), 0]

    def recall(self, key):
        """-> (value, counted here since) when read within ttl_s, else None."""
        e = self._seen.get(key)
        if e is None or time.time() - e[1] > self.ttl_s:
            return None
        return e[0], e[2]

    def count(self, key, n=1):
        """Count n here (while the store can't be written) on a remembered key."""
        if key in self._seen:
            self._seen[key][2] += n


def log_fallback(kind, tenant, allowed, error):
    """One line per decision taken without the store (a metric filter alarms on refusals)."""
    print(json.dumps({"limits_fallback": kind, "tenant": tenant, "decision": "allow" if allowed else "refuse",
                      "error": str(error)[:200]}))


UNAVAILABLE = "query limits can't be checked right now; try again in a few seconds"
