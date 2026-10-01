"""The AI SRE's loop, storage and API, against moto, with a scripted Claude and fake query results."""
import json
import os
import types

import boto3
import pytest
from moto import mock_aws

os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                  BUCKET="obs-data-test", TENANTS_TABLE="obs-tenants", CLAUDE_WORKSPACE_ID="wrkspc_test")
os.environ.pop("AWS_SESSION_TOKEN", None)


@pytest.fixture
def sre(monkeypatch):
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="obs-data-test")
        boto3.client("dynamodb").create_table(
            TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "tenant", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "by-tenant", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "tenant", "KeyType": "HASH"}, {"AttributeName": "pk", "KeyType": "RANGE"}]}])
        import importlib
        import sre as mod
        importlib.reload(mod)
        started = []
        monkeypatch.setitem(mod._clients, "lambda", types.SimpleNamespace(invoke=lambda **kw: started.append(json.loads(kw["Payload"]))))
        mod.started = started
        yield mod


class Resp:
    def __init__(self, content, stop):
        self.content, self.stop_reason = content, stop

    def to_dict(self):
        return {"content": self.content}


class Claude:
    """Plays back scripted responses; records every request."""
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        return self.script.pop(0)


def tool_use(i, name, **inp):
    return {"type": "tool_use", "id": f"tu_{i}", "name": name, "input": inp}


def api(mod, method, body=None, cid=None, tenant="acme", user="ana@acme.io"):
    out = mod.handler({"httpMethod": method, "pathParameters": {"proxy": cid} if cid else None, "body": json.dumps(body) if body else None,
                       "requestContext": {"authorizer": {"claims": {"custom:tenant": tenant, "email": user}}}}, None)
    return out["statusCode"], json.loads(out["body"])


def test_a_question_is_investigated_with_tools_and_answered(sre):
    s, conv = api(sre, "POST", {"message": "Why is checkout slow?", "context": {"start": "2026-10-01T08:00:00Z", "end": "2026-10-01T09:00:00Z"}})
    assert s == 202 and conv["status"] == "running" and sre.started == [{"run": {"tenant": "acme", "id": conv["id"]}}]
    claude = Claude([
        Resp([{"type": "thinking", "thinking": "Checking each service's latency.", "signature": "sig1"},
              tool_use(1, "list_services")], "tool_use"),
        Resp([{"type": "thinking", "thinking": "", "signature": "sig2"}, tool_use(2, "search_spans", service="checkout", min_duration_ms=500),
              tool_use(3, "run_sql", sql="SELECT 1")], "tool_use"),
        Resp([{"type": "text", "text": "Checkout is slow because `shipping` GetQuote takes 2 s."}], "end_turn")])
    calls = []

    class Tools:
        def run(self, name, args):
            calls.append((name, args))
            if name == "run_sql":
                raise ValueError("bad SQL: syntax error")
            return {"services": [{"service": "checkout", "p95_ms": 2100}]} if name == "list_services" else {"spans": [{"trace_id": "ab"}]}
    out = sre.run_turn("acme", conv["id"], claude=claude, tools=Tools())
    assert out["done"] == conv["id"] and [c[0] for c in calls] == ["list_services", "search_spans", "run_sql"]
    # The request: Opus 5.5 with progress updates, fallbacks, caching; the question carries time and range.
    r0 = claude.requests[0]
    assert r0["model"] == "claude-opus-5-5" and r0["fallbacks"] == "default" and r0["thinking"] == {"type": "adaptive", "display": "updates"}
    assert "server-side-fallback-2026-07-01" in r0["betas"] and r0["cache_control"] == {"type": "ephemeral"}
    assert "Time range on screen: 2026-10-01T08:00:00Z to 2026-10-01T09:00:00Z" in r0["messages"][0]["content"][0]["text"]
    # History is append-only: each request starts with the previous one's messages, unchanged.
    for a, b in zip(claude.requests, claude.requests[1:]):
        assert b["messages"][:len(a["messages"])] == a["messages"]
    # Both tool results went back in one message; the failing one as an error the model can fix.
    results = claude.requests[2]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_2", "tu_3"] and results[1]["is_error"] and "syntax error" in results[1]["content"]
    s, v = api(sre, "GET", cid=conv["id"])
    assert v["status"] == "done"
    assert [e["type"] for e in v["view"]] == ["question", "progress", "tool", "tool", "tool", "answer"]
    assert v["view"][2] == {"type": "tool", "name": "list_services", "input": {}, "summary": "1 services"}
    assert v["view"][4]["error"].startswith("bad SQL") and "messages" not in v      # never the raw API messages
    # A follow-up continues the same conversation; the list shows it.
    s, conv2 = api(sre, "POST", {"message": "And yesterday?", "conversation_id": conv["id"]})
    assert s == 202 and conv2["id"] == conv["id"]
    assert api(sre, "GET")[1]["conversations"][0]["title"] == "Why is checkout slow?"


def test_out_of_steps_it_answers_with_what_it_has(sre, monkeypatch):
    monkeypatch.setattr(sre, "MAX_STEPS", 2)
    s, conv = api(sre, "POST", {"message": "Investigate everything"})
    claude = Claude([Resp([tool_use(i, "list_services")], "tool_use") for i in range(3)]   # keeps asking for tools...
                    + [Resp([{"type": "text", "text": "So far: nothing unusual."}], "end_turn")])
    ran = []
    sre.run_turn("acme", conv["id"], claude=claude, tools=types.SimpleNamespace(run=lambda n, a: ran.append(n) or {"services": []}))
    assert len(ran) == 2                                         # the third call after the stop was refused
    stop = claude.requests[2]["messages"][-1]
    assert stop["role"] == "system" and "Stop investigating" in stop["content"]
    assert claude.requests[3]["messages"][-1]["content"][0]["is_error"]
    v = sre.view(sre.load("acme", conv["id"]))
    assert v["view"][-1] == {"type": "answer", "text": "So far: nothing unusual."} and v["status"] == "done"


def test_refusals_and_failures_are_shown(sre):
    s, conv = api(sre, "POST", {"message": "something"})
    sre.run_turn("acme", conv["id"], claude=Claude([Resp([], "refusal")]), tools=types.SimpleNamespace(run=None))
    assert sre.view(sre.load("acme", conv["id"]))["view"][-1]["type"] == "error"

    class Down:
        beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("overloaded"))))
    s, conv = api(sre, "POST", {"message": "again", "conversation_id": conv["id"]})
    with pytest.raises(RuntimeError):
        sre.run_turn("acme", conv["id"], claude=Down(), tools=None)
    v = sre.view(sre.load("acme", conv["id"]))
    assert v["status"] == "failed" and v["view"][-1]["text"].startswith("Something went wrong")


def test_conversations_belong_to_their_tenant_and_turns_are_limited(sre, monkeypatch):
    s, conv = api(sre, "POST", {"message": "hi"})
    assert api(sre, "GET", cid=conv["id"], tenant="globex")[0] == 404
    assert api(sre, "POST", {"message": "hi", "conversation_id": conv["id"]}, tenant="globex")[0] == 404
    assert api(sre, "POST", {"message": "more", "conversation_id": conv["id"]})[0] == 409     # still answering
    assert api(sre, "GET", tenant="globex")[1]["conversations"] == []
    assert api(sre, "POST", {"message": ""})[0] == 400 and api(sre, "POST", {"message": "x" * 5000})[0] == 400
    monkeypatch.setitem(sre.TURNS_PER_DAY, "standard", 2)
    assert api(sre, "POST", {"message": "two"})[0] == 202
    s, e = api(sre, "POST", {"message": "three"})
    assert s == 429 and "2 questions today" in e["error"]
    monkeypatch.setattr(sre, "WORKSPACE", "")
    assert api(sre, "POST", {"message": "x"}, tenant="other")[0] == 503


def test_tools_always_query_the_tenant_and_shape_results(sre):
    seen = []
    def invoke(p):
        seen.append(p)
        if "promql" in p and "time" in p:
            k = p["promql"]
            v = "2" if "rate(" in k else "100" if "ERROR" not in k else "5"
            v = "250" if "histogram_quantile" in k else v
            return {"data": {"resultType": "vector", "result": [{"metric": {"service_name": "checkout"}, "value": [0, v]}]}}
        if "promql" in p:
            return {"data": {"resultType": "matrix", "result": [{"metric": {"service_name": "checkout"},
                                                                 "values": [[1790841600 + 60 * i, str(i)] for i in range(100)]}]}}
        return {"columns": ["ts", "service", "body", "severity_text", "trace_id", "attributes"],
                "rows": [["2026-10-01T08:59:00Z", "checkout", "boom " * 300, "ERROR", "ab" * 16, {"k": "v"}]]}
    from datetime import datetime, timezone
    t = sre.Tools("acme", datetime(2026, 10, 1, 8, tzinfo=timezone.utc), datetime(2026, 10, 1, 9, tzinfo=timezone.utc), invoke=invoke)
    out = t.run("list_services", {})
    assert out["services"] == [{"service": "checkout", "requests_per_s": 2.0, "spans": 100, "error_pct": 5.0, "p95_ms": 250.0}]
    q = t.run("query_promql", {"promql": "sum(rate(leasyd.spans[5m]))"})
    assert q["series"][0]["points"] == 100 and len(q["series"][0]["sample"]) == 24 and q["series"][0]["max"] == 99
    logs = t.run("search_logs", {"service": "checkout", "min_severity": "ERROR", "text": "boom"})
    assert len(logs["records"][0]["body"]) == 601                      # long bodies cut
    assert all(p["tenant"] == "acme" for p in seen)
    w = seen[-1]["where"]
    assert {"field": "severity_number", "op": ">=", "value": 17} in w and {"field": "body", "op": "contains", "value": "boom"} in w
    with pytest.raises(ValueError):
        t.run("get_trace", {"trace_id": "not-hex"})
    with pytest.raises(ValueError):
        t.run("drop_tables", {})
