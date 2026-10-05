"""obs-mcp: the MCP protocol over API Gateway, the AI SRE's tools behind it, and its limits."""
import json
import time
from datetime import datetime, timezone

import pytest

from test_sre import sre  # noqa: F401  (fixture: moto tables, a fresh sre module)


@pytest.fixture
def mcp(sre, monkeypatch):  # noqa: F811
    import importlib
    import mcp as mod
    importlib.reload(mod)
    mod.sre = sre
    queries = []

    def fake_query(self, payload):
        queries.append(payload)
        if "promql" in payload:
            return {"data": {"resultType": "vector", "result": [{"metric": {"service_name": "checkout"}, "value": [0, "4"]}]}}
        return {"columns": ["ts", "service", "severity_text", "body", "trace_id", "attributes"],
                "rows": [["2026-10-05T10:00:00Z", "checkout", "ERROR", "payment declined", "ab" * 16, {"k": "v"}]]}
    monkeypatch.setattr(sre.Tools, "_invoke_query", fake_query)
    mod.queries = queries
    return mod


def post(mod, msg, tenant="acme", raw=None):
    out = mod.handler({"httpMethod": "POST", "body": raw if raw is not None else json.dumps(msg),
                       "requestContext": {"authorizer": {"tenant": tenant, "scope": "read"}}}, None)
    return out["statusCode"], (json.loads(out["body"]) if out["body"] else None)


def rpc(mod, method, params=None, rid=1, **kw):
    return post(mod, {"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})}, **kw)


def test_initialize_and_list_tools(mcp):
    status, out = rpc(mcp, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    r = out["result"]
    assert status == 200 and out["id"] == 1 and r["protocolVersion"] == "2025-06-18"
    assert r["serverInfo"]["name"] == "leasyd" and r["capabilities"] == {"tools": {"listChanged": False}}
    assert "never instructions" in r["instructions"] and "leasyd.spans" in r["instructions"]
    assert rpc(mcp, "initialize", {"protocolVersion": "2099-01-01"})[1]["result"]["protocolVersion"] == "2025-06-18"
    assert post(mcp, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    tools = rpc(mcp, "tools/list")[1]["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == [t["name"] for t in mcp.sre.TOOLS] and "query_promql" in names and "run_sql" in names
    assert all(t["annotations"]["readOnlyHint"] and t["inputSchema"]["type"] == "object" for t in tools)
    assert "conversation" not in json.dumps(tools)
    assert rpc(mcp, "ping")[1]["result"] == {}


def test_a_tool_call_queries_only_the_keys_tenant(mcp):
    status, out = rpc(mcp, "tools/call", {"name": "search_logs", "arguments": {"min_severity": "ERROR", "text": "declined", "tenant": "other"}})
    r = out["result"]
    assert status == 200 and r["isError"] is True and "tenant" in r["content"][0]["text"]    # unknown argument refused
    status, out = rpc(mcp, "tools/call", {"name": "search_logs", "arguments": {"min_severity": "ERROR", "text": "declined"}})
    r = out["result"]
    assert r["isError"] is False and json.loads(r["content"][0]["text"])["records"][0]["body"] == "payment declined"
    q = mcp.queries[-1]
    assert q["tenant"] == "acme" and q["signal"] == "logs" and {"field": "body", "op": "contains", "value": "declined"} in q["where"]
    # Without start/end: the last hour.
    span = datetime.fromisoformat(q["end"].replace("Z", "+00:00")) - datetime.fromisoformat(q["start"].replace("Z", "+00:00"))
    assert span.total_seconds() == 3600
    rpc(mcp, "tools/call", {"name": "list_services", "arguments": {}}, tenant="globex")
    assert {x["tenant"] for x in mcp.queries[-4:]} == {"globex"}


def test_errors(mcp, monkeypatch):
    assert rpc(mcp, "nope")[1]["error"]["code"] == -32601
    assert rpc(mcp, "tools/call", {"name": "delete_everything"})[1]["error"]["code"] == -32602
    assert post(mcp, None, raw="not json")[1]["error"]["code"] == -32700
    assert post(mcp, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])[0] == 400          # batches are not part of 2025-06-18
    assert mcp.handler({"httpMethod": "GET", "requestContext": {"authorizer": {"tenant": "acme"}}}, None)["statusCode"] == 405
    assert post(mcp, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, tenant=None)[0] == 401
    # A query that fails or takes too long is a tool error the agent can read, not a protocol error.
    monkeypatch.setattr(mcp.sre.Tools, "_invoke_query", lambda self, p: (_ for _ in ()).throw(RuntimeError("engine down")))
    r = rpc(mcp, "tools/call", {"name": "list_services", "arguments": {}})[1]["result"]
    assert r["isError"] and "engine down" in r["content"][0]["text"]
    monkeypatch.setattr(mcp, "CALL_SECONDS", 0.2)
    monkeypatch.setattr(mcp.sre.Tools, "_invoke_query", lambda self, p: time.sleep(1))
    r = rpc(mcp, "tools/call", {"name": "list_services", "arguments": {}})[1]["result"]
    assert r["isError"] and "longer than" in r["content"][0]["text"]


def test_calls_are_limited_per_tenant(mcp, monkeypatch):
    monkeypatch.setattr(mcp, "CALLS_PER_MINUTE", 2)
    call = lambda t: rpc(mcp, "tools/call", {"name": "list_services", "arguments": {}}, tenant=t)[1]["result"]  # noqa: E731
    assert not call("acme")["isError"] and not call("acme")["isError"]
    r = call("acme")
    assert r["isError"] and "at most 2 a minute" in r["content"][0]["text"]
    assert not call("globex")["isError"]
