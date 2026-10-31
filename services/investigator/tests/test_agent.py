"""The Claude agent's loop with a scripted model and fake telemetry: what it sends, what it accepts as a
finding, and the limits it enforces in code."""
import json
from datetime import datetime, timezone

from agent.claude_agent import ClaudeAgent
from agent.prompts import BuiltIn
from agent.redact import redact
from domain.model import Investigation, Trigger

NOW = datetime(2026, 10, 30, 2, 40, tzinfo=timezone.utc)


class Script:
    """Plays back model turns; records each request."""
    model = "claude-test"

    def __init__(self, turns):
        self.turns, self.requests = list(turns), []

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw)))
        return self.turns.pop(0) if self.turns else {"content": [{"type": "text", "text": "..."}], "usage": {}}


def use(i, name, **inp):
    return {"type": "tool_use", "id": f"tu{i}", "name": name, "input": inp}


def turn(*blocks, tokens=1000):
    return {"content": list(blocks), "stop_reason": "tool_use", "usage": {"input_tokens": tokens, "output_tokens": tokens // 10}}


class Telemetry:
    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def run(self, name, args):
        self.calls.append((name, args))
        if name == "fail":
            raise RuntimeError("boom")
        return self.answers.get(name, {"records": []})


def inv(details="error rate 6.2% > 5%"):
    return Investigation(id="a" * 16, tenant="acme", status="running", created_at="t", updated_at="t",
                         trigger=Trigger(source="alert", title="Checkout errors: checkout", dedupe_key="k",
                                         started_at="2026-10-30T02:14:00Z", subject="checkout", details=details))


def agent(script, tel, **kw):
    made = []

    def telemetry_for(tenant, start, end):
        made.append((tenant, start, end))
        return tel
    a = ClaudeAgent(script, telemetry_for, BuiltIn(), "https://app.leasyd.com", {"input": 5, "output": 25}, now=lambda: NOW, **kw)
    a.made = made
    return a


GOOD = {"summary": "checkout fails because payment times out calling fraud-api.", "root_cause": "payment times out calling fraud-api",
        "confidence": 0.82, "evidence": [{"id": "ev1", "claim": "payment errors 6.1% from 02:10"}, {"id": "ev2", "claim": "312 of 340 failed traces end in fraud-api timeouts"}],
        "ruled_out": ["database: latency normal"], "next_step": "Roll back payment to 2.4.0.", "safe_to_auto_act": True}


def test_a_finding_cites_the_tools_evidence_with_links_and_costs_are_logged():
    tel = Telemetry({"list_services": {"services": [{"service": "payment", "error_pct": 6.1}]},
                     "search_spans": {"spans": [{"trace_id": "t1"}]}})
    s = Script([turn(use(1, "list_services")), turn(use(2, "query_traces", mode="search", service="payment", errors_only=True)),
                turn(use(3, "submit_investigation", **GOOD))])
    a = agent(s, tel)
    i = inv()
    r = a.investigate(i)
    assert r.root_cause == "payment times out calling fraud-api" and r.confidence == 0.82 and r.safe_to_auto_act is False
    assert [(e.id, e.claim) for e in r.evidence] == [("ev1", "payment errors 6.1% from 02:10"), ("ev2", "312 of 340 failed traces end in fraud-api timeouts")]
    assert r.evidence[0].link.startswith("https://app.leasyd.com/#/apm?range=c")
    assert r.evidence[1].link.startswith("https://app.leasyd.com/#/traces?service=payment&range=c")
    # tenant and window bound by the agent, not the model: an hour before the start to 20 minutes after
    assert a.made == [("acme", datetime(2026, 10, 30, 1, 14, tzinfo=timezone.utc), datetime(2026, 10, 30, 2, 34, tzinfo=timezone.utc))]
    assert tel.calls[1] == ("search_spans", {"service": "payment", "errors_only": True, "start": "2026-10-30T01:14:00Z", "end": "2026-10-30T02:34:00Z"})
    assert [c["name"] for c in i.tool_calls] == ["list_services", "query_traces"] and i.tool_calls[1]["evidence"] == "ev2"
    assert i.cost["calls"] == 3 and i.cost["input_tokens"] == 3000 and i.cost["usd"] == round(3000 / 1e6 * 5 + 300 / 1e6 * 25, 4)
    assert a.versions == {"model": "claude-test", "prompt": BuiltIn().load()["version"], "tools": "telemetry-2"}
    first = s.requests[0]
    assert "<alert>" in first["messages"][0]["content"] and {t["name"] for t in first["tools"]} == {
        "list_services", "query_metrics", "query_logs", "query_traces", "get_recent_changes", "get_service_context", "submit_investigation"}
    assert '<evidence id="ev1">' in s.requests[1]["messages"][2]["content"][0]["content"]


def test_an_invented_evidence_id_or_an_uncited_cause_is_sent_back():
    s = Script([turn(use(1, "list_services")),
                turn(use(2, "submit_investigation", **{**GOOD, "evidence": [{"id": "ev9", "claim": "made up"}]})),
                turn(use(3, "submit_investigation", **{**GOOD, "evidence": [{"id": "ev1", "claim": "payment errors"}]}))])
    r = agent(s, Telemetry()).investigate(inv())
    back = s.requests[2]["messages"][-1]["content"][0]
    assert back["is_error"] and "'ev9' is not one the tools returned (you have: ev1)" in back["content"]
    assert [e.id for e in r.evidence] == ["ev1"] and r.confidence == 0.82
    s = Script([turn(use(1, "submit_investigation", **{**GOOD, "evidence": []})),
                turn(use(2, "submit_investigation", **{**GOOD, "evidence": [], "root_cause": "not sure", "confidence": 0.9}))])
    r = agent(s, Telemetry()).investigate(inv())
    assert "must cite evidence" in s.requests[1]["messages"][-1]["content"][0]["content"]
    assert r.root_cause == "not sure" and r.confidence == 0.3                     # "not sure" is never confident


def test_two_bad_submissions_end_in_not_sure_with_the_evidence_gathered():
    bad = {**GOOD, "evidence": [{"id": "ev7", "claim": "x"}]}
    s = Script([turn(use(1, "list_services")), turn(use(2, "submit_investigation", **bad)), turn(use(3, "submit_investigation", **bad))])
    r = agent(s, Telemetry()).investigate(inv())
    assert r.root_cause == "not sure" and r.confidence == 0 and [e.id for e in r.evidence] == ["ev1"]
    assert "no valid finding" in r.summary


def test_limits_force_a_submission_then_give_up():
    loop = [turn(use(i, "list_services")) for i in range(3)]      # the 4th call is forced to submit
    s = Script(loop + [turn(use(99, "submit_investigation", **{**GOOD, "evidence": [{"id": "ev2", "claim": "x"}]}))])
    i = inv()
    r = agent(s, Telemetry(), max_steps=3).investigate(i)
    last = s.requests[-1]
    assert len(s.requests) == 4 and last["tool_choice"] == {"type": "tool", "name": "submit_investigation"}
    assert [t["name"] for t in last["tools"]] == ["submit_investigation"] and "Stop now (step limit)" in json.dumps(last["messages"])
    assert r.confidence == 0.82 and i.cost["stopped"] == "step limit"
    s = Script([turn(use(1, "list_services"), tokens=600_000), turn({"type": "text", "text": "I think it's the database."})])
    r = agent(s, Telemetry()).investigate(inv())
    assert r.root_cause == "not sure" and "token budget" in r.summary and len(s.requests) == 2
    clock = iter([0, 0, 400, 400, 400])
    a = agent(Script([turn(use(1, "list_services"))]), Telemetry(), time_limit_s=300)
    a.clock = lambda: next(clock)
    assert "time limit" in a.investigate(inv()).summary


def test_secrets_and_personal_data_never_reach_the_model():
    tel = Telemetry({"search_logs": {"records": [{"body": "login failed for ana@acme.io token=sk_live_abcdefghijklmnop1234 "
                                                          "card 4111 1111 1111 1111 order 1234567890123 from 10.0.3.7 key AKIAABCDEFGHIJKLMNOP"}]}})
    s = Script([turn(use(1, "query_logs", mode="search", text="failed")), turn(use(2, "submit_investigation", **{**GOOD, "evidence": [{"id": "ev1", "claim": "x"}]}))])
    i = inv(details="Authorization: Bearer abcdefghijklmnopqrstuvwxyz from bo@acme.io")
    agent(s, tel).investigate(i)
    sent = json.dumps(s.requests)
    for secret in ("ana@acme.io", "sk_live_abcdefghijklmnop1234", "4111 1111 1111 1111", "AKIAABCDEFGHIJKLMNOP", "bo@acme.io", "abcdefghijklmnopqrstuvwxyz"):
        assert secret not in sent, secret
    assert "1234567890123" in sent and "10.0.3.7" in sent                    # an order number and an IP stay: they are evidence
    assert i.tool_calls[0]["redacted"] == {"api_key": 1, "email": 1, "card": 1, "aws_key": 1}


def test_tool_errors_go_back_to_the_model_and_are_logged():
    s = Script([turn(use(1, "query_traces", mode="trace")), turn(use(2, "query_logs", mode="search", start="2026-09-01T00:00:00Z")),
                turn(use(3, "drop_tables")), turn(use(4, "submit_investigation", **{**GOOD, "evidence": [], "root_cause": "not sure", "confidence": 0.1}))])
    i = inv()
    r = agent(s, Telemetry()).investigate(i)
    errors = [c["error"] for c in i.tool_calls]
    assert errors == ["mode trace needs trace_id", "stay within a day of the incident's window", "unknown tool drop_tables"]
    assert r.root_cause == "not sure" and s.requests[1]["messages"][-1]["content"][0]["is_error"]


def test_redact_patterns():
    text, found = redact("postgres://app:hunter2@db:5432/x Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl "
                         "password=pa55word ghp_abcdefghijklmnopqrstuvwxyz0123456789 call +44 20 7946 0958")
    assert "hunter2" not in text and "YWxhZGRpbjpvcGVuc2VzYW1l" not in text and "eyJzdWIiOiIxIn0" not in text and "pa55word" not in text
    assert "ghp_" not in text and "7946 0958" not in text and "postgres://" in text and "@db:5432" in text
    assert redact("trace 4bf92f3577b34da6a3ce929d0e0e4736 took 1234 ms")[0] == "trace 4bf92f3577b34da6a3ce929d0e0e4736 took 1234 ms"


class Summaries(Telemetry):
    """Answers the change tools' SQL over the per-minute summaries, and is the Deploys port."""

    def __init__(self, versions=(), edges=(), deploys=()):
        super().__init__()
        self.versions, self.edges, self.recorded, self.asked = list(versions), list(edges), list(deploys), []

    def run(self, name, args):
        self.calls.append((name, args))
        if "edge_1m" in args["sql"]:
            return {"columns": ["source", "target", "calls", "errors"], "rows": self.edges}
        return {"columns": ["service", "version", "first_seen", "last_seen", "requests", "errors"], "rows": self.versions}

    def between(self, start, end, service=None):
        self.asked.append((start, end, service))
        return self.recorded


def test_recent_changes_bring_recorded_deploys_and_new_versions_from_the_day_before():
    tel = Summaries(versions=[["cart", "2.4.3", "2026-10-29T01:14:00Z", "2026-10-30T02:12:00Z", 900, 1],
                              ["cart", "2.5.0", "2026-10-30T02:12:00Z", "2026-10-30T02:34:00Z", 300, 90]],
                    deploys=[{"id": "d1", "service": "cart", "version": "2.5.0", "deployed_at": "2026-10-30T02:11:00Z"}])
    s = Script([turn(use(1, "get_recent_changes")), turn(use(2, "submit_investigation", **{**GOOD, "evidence": [{"id": "ev1", "claim": "x"}]}))])
    a = agent(s, tel, deploys_for=lambda tenant: (tenant == "acme" or pytest_fail()) and tel)
    i = inv()
    r = a.investigate(i)
    sent = s.requests[1]["messages"][2]["content"][0]["content"]
    assert '"recorded_deploys": [{"id": "d1"' in sent and '"previous": "2.4.3"' in sent and '"version": "2.5.0"' in sent
    assert tel.asked == [("2026-10-29T01:14:00Z", "2026-10-30T02:34:00Z", None)]          # a day before the window
    assert "FROM svc_1m" in tel.calls[0][1]["sql"] and tel.calls[0][1]["start"] == "2026-10-29T01:14:00Z"
    assert r.evidence[0].kind == "changes" and r.evidence[0].summary == "1 recorded deploys, 1 version changes"


def test_no_change_is_said_plainly_and_names_are_quoted_safely():
    tel = Summaries(versions=[["cart", "2.4.3", "2026-10-29T01:14:00Z", "2026-10-30T02:34:00Z", 900, 1]])
    s = Script([turn(use(1, "get_recent_changes", service="it's"), use(2, "get_service_context", service="cart")),
                turn(use(3, "submit_investigation", **{**GOOD, "root_cause": "not sure", "evidence": []}))])
    i = inv()
    agent(s, tel).investigate(i)                                                     # no Deploys port: inferred only
    results = s.requests[1]["messages"][2]["content"]
    assert "No deploy recorded and no version change seen for it's" in results[0]["content"]
    assert "service = 'it''s'" in tel.calls[0][1]["sql"]
    assert '"versions": [{"version": "2.4.3"' in results[1]["content"] and "edge_1m" in tel.calls[2][1]["sql"]
    assert [c["evidence"] for c in i.tool_calls] == ["ev1", "ev2"]
    assert i.result is None and results[1]["content"].startswith('<evidence id="ev2">')


def pytest_fail():
    raise AssertionError("deploys asked for another tenant")
