"""AI agent spans end to end (docs/agents-design.md): ingested as the fast lane files them, then the
queries the Agents page makes: aggregates over the agent columns, and one session's spans by
session_id, which the per-file blooms narrow to the files that can hold it."""
from datetime import datetime, timezone

import pytest

T0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
NS = int(T0.timestamp()) * 10**9
WINDOW = {"start": "2026-10-19T09:00:00Z", "end": "2026-10-19T12:00:00Z"}


def kv(k, v):
    return {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}


def spans(session, trace, at_s, n_chat=2, tool_error=False):
    """An agent run: invoke_agent (with the session ID), chat calls and one tool call (without it,
    as many SDKs leave the session on the root span only)."""
    t, root = NS + at_s * 10**9, trace[:16]
    out = [{"traceId": trace, "spanId": root, "name": "invoke_agent support", "kind": 1,
            "startTimeUnixNano": str(t), "endTimeUnixNano": str(t + 3 * 10**9),
            "attributes": [kv("gen_ai.operation.name", "invoke_agent"), kv("gen_ai.agent.name", "support"),
                           kv("gen_ai.conversation.id", session)]}]
    for i in range(n_chat):
        out.append({"traceId": trace, "spanId": f"{i + 1:016x}", "parentSpanId": root, "name": "chat gpt-4o-mini",
                    "kind": 3, "startTimeUnixNano": str(t + i), "endTimeUnixNano": str(t + 10**9),
                    "attributes": [kv("gen_ai.operation.name", "chat"), kv("gen_ai.request.model", "gpt-4o-mini"),
                                   kv("gen_ai.usage.input_tokens", 1000), kv("gen_ai.usage.output_tokens", 100),
                                   kv("gen_ai.input.messages", '[{"role":"user","parts":[{"type":"text","content":"hi"}]}]')]})
    out.append({"traceId": trace, "spanId": "f" * 16, "parentSpanId": root, "name": "execute_tool lookup_order",
                "kind": 1, "startTimeUnixNano": str(t + 5), "endTimeUnixNano": str(t + 6),
                "attributes": [kv("gen_ai.operation.name", "execute_tool"), kv("gen_ai.tool.name", "lookup_order")],
                **({"status": {"code": 2, "message": "timeout"}} if tool_error else {})})
    return {"resourceSpans": [{"resource": {"attributes": [kv("service.name", "support-bot")]},
                               "scopeSpans": [{"scope": {"name": "agents"}, "spans": out}]}]}


@pytest.fixture(scope="module")
def eng():
    from localengine import LocalEngine
    e = LocalEngine()
    e.load("traces", [spans("sess-a", "a" * 32, 0), spans("sess-a", "b" * 32, 60)], T0)
    e.load("traces", [spans("sess-b", "c" * 32, 900, n_chat=3, tool_error=True)], T0.replace(minute=15))
    e.load("traces", [{"resourceSpans": [{"resource": {"attributes": [kv("service.name", "shop")]},
                                         "scopeSpans": [{"scope": {"name": "x"}, "spans": [
                                             {"traceId": "d" * 32, "spanId": "1" * 16, "name": "GET /",
                                              "startTimeUnixNano": str(NS), "endTimeUnixNano": str(NS + 1)}]}]}]}],
           T0.replace(minute=30))
    yield e
    e.close()


def groups(out):
    return [dict(zip(out["columns"], r)) for r in out["rows"]]


def test_sessions_aggregate_from_the_agent_columns(eng):
    # The Agents page's list query: per trace and session, so a trace's spans without the session ID
    # (chat, tool) still count towards it: the page merges a trace's groups.
    out = eng.query_json({**WINDOW, "signal": "traces", "where": [{"field": "gen_ai_operation", "op": "exists"}],
                     "group_by": ["trace_id", "session_id", "gen_ai_operation", "gen_ai_model", "status_code"],
                     "aggs": [{"fn": "count"}, {"fn": "sum", "field": "gen_ai_input_tokens"},
                              {"fn": "sum", "field": "gen_ai_output_tokens"}], "limit": 1000})
    g = groups(out)
    session_of = {r["trace_id"]: r["session_id"] for r in g if r["session_id"]}
    assert session_of == {"a" * 32: "sess-a", "b" * 32: "sess-a", "c" * 32: "sess-b"}
    tokens = {}
    for r in g:
        s = session_of[r["trace_id"]]
        tokens[s] = tokens.get(s, 0) + (r["sum(gen_ai_input_tokens)"] or 0)
    assert tokens == {"sess-a": 4000, "sess-b": 3000}
    errors = [(r["trace_id"], r["gen_ai_operation"]) for r in g if r["status_code"] == 2]
    assert errors == [("c" * 32, "execute_tool")]
    assert all(r["trace_id"] != "d" * 32 for r in g)   # the shop's span isn't an agent span


def test_one_session_by_id_reads_only_its_files(eng):
    found = eng.lookup.lookup(eng.tenant, WINDOW["start"], WINDOW["end"], "traces", match={"session_id": "SESS-B"})
    assert found["stats"]["in_time_range"] == 3 and len(found["files"]) == 1
    out = eng.query_json({**WINDOW, "signal": "traces", "match": {"session_id": "sess-b"}, "search": {"limit": 100}})
    assert [r["trace_id"] for r in groups(out)] == ["c" * 32]   # only the root span carries the session ID
    assert groups(out)[0]["gen_ai_agent"] == "support"
