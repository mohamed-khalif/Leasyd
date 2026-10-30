"""The core, with no AWS: the record's rules, the trigger parsers, and the workflow (dedupe, the
hourly cap, retries, idempotency) against in-memory ports."""
import json

import pytest

from agent.stub import StubAgent
from domain import triggers
from domain.model import Evidence, InvalidResult, InvalidTransition, Investigation, Result, Trigger
from domain.workflow import DEDUPE_S, open_investigation, run_investigation
from tests.fakes import Clock, ListQueue, MemoryStore


def trig(key="alert:r1:checkout", title="Checkout errors: checkout"):
    return Trigger(source="alert", title=title, dedupe_key=key, started_at="2026-10-30T02:14:00Z", subject="checkout", rule_id="r1")


# ------------------------------------------------------------------ the record

def test_states_move_only_forward_and_every_move_is_logged():
    inv = Investigation(id="a" * 16, tenant="acme", trigger=trig(), status="queued", created_at="t0", updated_at="t0")
    inv.transition("running", "t1")
    with pytest.raises(InvalidTransition):
        inv.transition("queued", "t2")
    inv.transition("completed", "t3")
    with pytest.raises(InvalidTransition):
        inv.transition("failed", "t4")
    assert [e["type"] for e in inv.events] == ["running", "completed"] and inv.final
    back = Investigation.from_dict(json.loads(json.dumps(inv.to_dict())))
    assert back == inv


def test_a_result_must_cite_evidence_and_never_allows_actions():
    with pytest.raises(InvalidResult):
        Result(title="x", summary="y", root_cause="payment v2.4.1", confidence=0.8).validate()   # no evidence
    with pytest.raises(InvalidResult):
        Result(title="x", summary="y", root_cause="z", confidence=1.5).validate()
    ev = Evidence(id="e1", kind="metrics", title="errors", summary="up", link="https://app.leasyd.com/#/apm/payment")
    with pytest.raises(InvalidResult):
        Result(title="x", summary="y", root_cause="z", confidence=0.5, evidence=[ev, ev]).validate()
    r = Result(title="x", summary="y", root_cause="z", confidence=0.5, evidence=[ev], safe_to_auto_act=True).validate()
    assert r.safe_to_auto_act is False
    assert Result(title="x", summary="y", root_cause="not sure", confidence=0).validate()   # "not sure" needs none


# ------------------------------------------------------------------ triggers

def test_leasyd_alert_firing_starts_one_resolved_does_not():
    p = {"tenant": "acme", "state": "firing", "rule": {"id": "r1", "name": "Checkout errors", "type": "query"},
         "subject": "checkout", "detail": "error rate 6.2% > 5%", "url": "https://app.leasyd.com/#/alerts/rules/r1",
         "time": "2026-10-30T02:14:05+00:00"}
    kind, t = triggers.from_alert(p)
    assert kind == "trigger" and t.dedupe_key == "alert:r1:checkout" and t.started_at == "2026-10-30T02:14:05Z"
    assert t.title == "Checkout errors: checkout" and t.rule_id == "r1" and "6.2%" in t.details
    assert triggers.from_alert({**p, "state": "resolved"})[0] == "ignore"


def test_cloudwatch_alarm_via_sns_and_its_subscription():
    alarm = {"AlarmName": "payment-5xx", "AlarmArn": "arn:aws:cloudwatch:us-east-1:1:alarm:payment-5xx", "NewStateValue": "ALARM",
             "NewStateReason": "Threshold Crossed: 1 datapoint [42.0] > 10", "StateChangeTime": "2026-10-30T02:14:00.000+0000",
             "Trigger": {"MetricName": "5XXError", "Namespace": "AWS/ApiGateway", "Dimensions": [{"name": "ServiceName", "value": "payment"}]}}
    kind, t = triggers.from_webhook({"Type": "Notification", "Message": json.dumps(alarm)})
    assert kind == "trigger" and t.source == "cloudwatch" and t.service == "payment"
    assert t.dedupe_key.endswith("alarm:payment-5xx") and t.started_at == "2026-10-30T02:14:00Z" and "5XXError" in t.details
    assert triggers.from_webhook({"Type": "Notification", "Message": json.dumps({**alarm, "NewStateValue": "OK"})})[0] == "ignore"
    ok = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=x&Token=y"
    assert triggers.from_webhook({"Type": "SubscriptionConfirmation", "SubscribeURL": ok}) == ("confirm", ok)
    for bad in ("http://sns.us-east-1.amazonaws.com/?x", "https://evil.example/?sns.us-east-1.amazonaws.com", "https://169.254.169.254/?"):
        with pytest.raises(ValueError):
            triggers.from_webhook({"Type": "SubscriptionConfirmation", "SubscribeURL": bad})


def test_pagerduty_v3_and_generic_payloads():
    pd = {"event": {"event_type": "incident.triggered", "occurred_at": "2026-10-30T02:14:00Z",
                    "data": {"id": "Q1", "title": "High latency on cart", "urgency": "high", "html_url": "https://x.pagerduty.com/incidents/Q1",
                             "service": {"summary": "cart"}}}}
    kind, t = triggers.from_webhook(pd)
    assert kind == "trigger" and t.source == "pagerduty" and t.service == "cart" and t.dedupe_key == "pagerduty:Q1"
    assert triggers.from_webhook({"event": {**pd["event"], "event_type": "incident.resolved"}})[0] == "ignore"
    kind, t = triggers.from_webhook({"title": "Queue backing up", "service": "orders", "severity": "warning"})
    assert kind == "trigger" and t.source == "generic" and t.dedupe_key == "generic:orders:Queue backing up"
    for bad in ({}, {"title": ""}, {"title": "x", "severity": "panic"}, []):
        with pytest.raises(ValueError):
            triggers.from_webhook(bad)


# ------------------------------------------------------------------ workflow

def setup():
    return MemoryStore(), ListQueue(), Clock()


def test_a_trigger_queues_one_investigation_and_repeats_join_it():
    store, q, clock = setup()
    inv, joined = open_investigation(store, q, clock, "acme", trig(), "a" * 16)
    assert not joined and inv.status == "queued" and q.sent == [{"run": {"tenant": "acme", "id": "a" * 16}}]
    clock.t += 60
    again, joined = open_investigation(store, q, clock, "acme", trig(), "b" * 16)
    assert joined and again.id == "a" * 16 and len(q.sent) == 1
    assert [e["type"] for e in store.get("acme", "a" * 16).events] == ["triggered", "trigger_joined"]
    clock.t += DEDUPE_S + 1                                   # after the window: a new one
    assert open_investigation(store, q, clock, "acme", trig(), "c" * 16)[0].id == "c" * 16
    other, _ = open_investigation(store, q, clock, "globex", trig(), "d" * 16)   # same key, other tenant: its own
    assert other.id == "d" * 16 and store.get("acme", "d" * 16) is None


def test_over_the_hourly_cap_it_is_skipped_visibly_not_dropped():
    store, q, clock = setup()
    for i in range(3):
        open_investigation(store, q, clock, "acme", trig(key=f"k{i}"), f"{i:016x}", cap=2)
    skipped = store.get("acme", f"{2:016x}")
    assert skipped.status == "skipped" and skipped.skip_reason == "rate_limit"
    assert q.sent[-1] == {"deliver": {"tenant": "acme", "id": f"{2:016x}"}}       # still delivered, as a notice
    assert [m for m in q.sent if "run" in m] == [{"run": {"tenant": "acme", "id": f"{i:016x}"}} for i in range(2)]


def test_run_completes_once_even_if_the_message_comes_twice():
    store, q, clock = setup()
    open_investigation(store, q, clock, "acme", trig(), "a" * 16)
    assert run_investigation(store, StubAgent(), q, clock, "acme", "a" * 16) == "completed"
    inv = store.get("acme", "a" * 16)
    assert inv.status == "completed" and inv.result.root_cause == "not sure" and inv.result.confidence == 0
    assert inv.versions["agent"] == "stub" and inv.attempts == 1
    assert run_investigation(store, StubAgent(), q, clock, "acme", "a" * 16) == "done already"
    assert [m for m in q.sent if "deliver" in m] == [{"deliver": {"tenant": "acme", "id": "a" * 16}}]
    assert run_investigation(store, StubAgent(), q, clock, "acme", "f" * 16) == "missing"


class Flaky:
    name, versions = "flaky", {}

    def __init__(self, fails):
        self.fails = fails

    def investigate(self, inv):
        if self.fails:
            self.fails -= 1
            raise TimeoutError("model timed out")
        return StubAgent().investigate(inv)


def test_a_failed_attempt_is_retried_and_the_last_one_marks_it_failed():
    store, q, clock = setup()
    open_investigation(store, q, clock, "acme", trig(), "a" * 16)
    agent = Flaky(fails=1)
    with pytest.raises(TimeoutError):
        run_investigation(store, agent, q, clock, "acme", "a" * 16, attempt=1)
    assert store.get("acme", "a" * 16).status == "running"
    assert run_investigation(store, agent, q, clock, "acme", "a" * 16, attempt=2) == "completed"
    assert [e["type"] for e in store.get("acme", "a" * 16).events] == ["triggered", "running", "attempt_failed", "retry", "completed"]

    open_investigation(store, q, clock, "acme", trig(key="k2"), "b" * 16)
    agent = Flaky(fails=9)
    for attempt in (1, 2):
        with pytest.raises(TimeoutError):
            run_investigation(store, agent, q, clock, "acme", "b" * 16, attempt=attempt)
    assert run_investigation(store, agent, q, clock, "acme", "b" * 16, attempt=3) == "failed"
    assert store.get("acme", "b" * 16).status == "failed" and q.sent[-1] == {"deliver": {"tenant": "acme", "id": "b" * 16}}


def test_an_invalid_result_is_a_failed_attempt_not_a_delivered_claim():
    class Inventing:
        name, versions = "inventing", {}

        def investigate(self, inv):
            return Result(title="x", summary="y", root_cause="the database", confidence=0.9)   # cites nothing
    store, q, clock = setup()
    open_investigation(store, q, clock, "acme", trig(), "a" * 16)
    assert run_investigation(store, Inventing(), q, clock, "acme", "a" * 16, attempt=3) == "failed"
    assert "must cite evidence" in store.get("acme", "a" * 16).events[-2]["detail"]


def test_a_second_worker_with_a_stale_copy_stands_down():
    store, q, clock = setup()
    open_investigation(store, q, clock, "acme", trig(), "a" * 16)
    stale = store.get("acme", "a" * 16)
    fresh = store.get("acme", "a" * 16)
    fresh.log("t", "note")
    store.save(fresh)
    stale_store = type("S", (), {"get": lambda self, t, i: stale, "save": store.save})()
    assert run_investigation(stale_store, StubAgent(), q, clock, "acme", "a" * 16) == "busy"
