"""Memory: which past investigations are like a new one, and how a wrong one is flagged."""
from datetime import datetime, timezone

from domain.memory import similar
from domain.model import Trigger

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
T = Trigger(source="alert", title="Cart p95 above 1s: cart", dedupe_key="k", started_at="2026-11-08T11:50:00Z", subject="cart",
            service="cart", rule_id="r9")


def row(i, **kw):
    return {"id": i, "status": "completed", "title": "x", "created_at": "2026-11-01T00:00:00Z", "root_cause": "rc", **kw}


def test_the_same_rule_or_service_ranks_first_and_unfinished_or_unrelated_ones_are_left_out():
    rows = [row("rule", rule_id="r9"), row("svc", service="cart", title="Cart p95 above 1s: cart"), row("words", title="cart p95 slow redis"),
            row("other", service="search", title="Search errors"), row("running", status="running", rule_id="r9"),
            row("no-cause", rule_id="r9", root_cause=None), row("self", rule_id="r9")]
    got = [x["id"] for x in similar(rows, T, NOW, exclude_id="self")]
    assert got[:2] == ["svc", "rule"] and "other" not in got and "running" not in got and "no-cause" not in got and "self" not in got


def test_a_wrong_finding_is_flagged_with_what_the_team_said():
    lead = similar([row("w", rule_id="r9", verdict="wrong", note="it was the CDN")], T, NOW)[0]
    assert lead["team_verdict"] == "wrong" and lead["team_note"] == "it was the CDN" and "WRONG" in lead["warning"]
    assert similar([row("u", rule_id="r9")], T, NOW)[0]["team_verdict"] == "not rated"


def test_search_by_service_or_text():
    rows = [row("a", service="payment", title="Payment errors"), row("b", title="redis timeouts in cart", service="checkout")]
    assert [x["id"] for x in similar(rows, T, NOW, service="payment")] == ["a"]
    assert [x["id"] for x in similar(rows, T, NOW, text="redis timeouts")][0] == "b"
