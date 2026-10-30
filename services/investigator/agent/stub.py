"""The placeholder agent (slice 1): investigates nothing and says so. Replaced by the Claude agent in
slice 2; kept for tests and as a safe fallback (AGENT=stub)."""

from domain.model import Result


class StubAgent:
    name = "stub"
    versions = {"prompt": "none", "tools": "none"}

    def investigate(self, inv):
        return Result(title=inv.trigger.title, summary="Not investigated yet: the automatic investigation isn't switched on.",
                      root_cause="not sure", confidence=0.0, next_step="Look at the alert in Leasyd.")
