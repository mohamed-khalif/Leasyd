"""Demo AI agent traffic: the shop's customer-support assistant, as OpenTelemetry GenAI spans
(gen_ai.* conventions), for the Agents page (docs/agents-design.md).

Each session is one customer's conversation over a few minutes, one turn (a trace) a minute:
invoke_agent (with gen_ai.conversation.id) -> chat (the model, with tokens and the messages) ->
execute_tool calls -> a final chat with the reply. Seeded by the session and minute, so a minute's
spans are the same every time. The failures the Agents page should find, at realistic rates:
  - initiateReturn fails (DeadlockDetected) and the agent tells the customer the return was filed
  - checkInventory fails (product_not_found) and the agent still answers
  - an LLM call is rate limited (429)
  - an empty reply (0 output tokens)
  - a loop: searchProducts called again and again in one turn
"""
import hashlib
import json
import random

SERVICE = "support-assistant"
AGENT = "order_support_agent"
SESSIONS_PER_WINDOW = 9      # conversations starting in each 6-minute window
WINDOW_MIN = 6
MODELS = ("gpt-4o-mini", "claude-sonnet-4")
SYSTEM = "You are the shop's support assistant. Use the tools; never promise what a tool didn't confirm."

ASKS = [
    ("return", "My order #{o} arrived broken, the camera doesn't turn on. Can I return it?"),
    ("status", "Where is my order #{o}? It said it would arrive yesterday."),
    ("stock", "Do you have the {p} in stock? I want to order two."),
    ("recommend", "I need a gift for my dad who likes hiking, under $80. Ideas?"),
]
PRODUCTS = ["trail backpack", "insulated bottle", "headlamp", "camp stove", "trekking poles"]
FOLLOW_UPS = ["Thanks. When will I get the label?", "Ok, and how long does the refund take?",
              "Great, can you also check order #{o2}?", "Perfect, thank you!"]


def _id(*parts, n=32):
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[:n]


def _kv(d):
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            out.append({"key": k, "value": {"doubleValue": v}})
        else:
            out.append({"key": k, "value": {"stringValue": v if isinstance(v, str) else json.dumps(v)}})
    return out


def _text(role, text):
    return {"role": role, "parts": [{"type": "text", "content": text}]}


def _call(name, args, cid):
    return {"role": "assistant", "parts": [{"type": "tool_call", "id": cid, "name": name, "arguments": args}]}


def _tool_reply(cid, result):
    return {"role": "tool", "parts": [{"type": "tool_call_response", "id": cid, "response": result}]}


def _session(window, k):
    """A conversation's fixed traits: its ID, model, what the customer wants, how many turns."""
    r = random.Random(f"agents/session/{window}/{k}")
    kind, ask = r.choice(ASKS)
    product = r.choice(PRODUCTS)
    return {"id": f"{_id('sess', window, k, n=8)}-{_id('s2', window, k, n=4)}-{_id('s3', window, k, n=12)}",
            "model": MODELS[0] if r.random() < 0.7 else MODELS[1], "kind": kind,
            "first": ask.format(o=r.randint(4000, 4999), p=product), "product": product, "turns": r.randint(1, 4),
            "start": window * WINDOW_MIN + r.randint(0, WINDOW_MIN - 1), "user": f"user-{r.randint(100, 999)}"}


def _turn(s, turn, minute):
    """One turn of a session -> its spans (one trace)."""
    r = random.Random(f"agents/turn/{s['id']}/{turn}")
    trace = _id("agent-trace", s["id"], turn)
    t0 = minute * 60 * 10**9 + r.randint(0, 50) * 10**9
    spans, t = [], t0
    root = trace[:16]
    user = s["first"] if turn == 0 else r.choice(FOLLOW_UPS).format(o2=r.randint(4000, 4999))
    history = [_text("system", SYSTEM), _text("user", user)]

    def span(name, op, start, dur_ms, attrs, error=None, parent=root, sid=None):
        sp = {"traceId": trace, "spanId": sid or _id(trace, name, start, n=16), "parentSpanId": parent, "name": name,
              "kind": 3 if op == "chat" else 1, "startTimeUnixNano": str(start),
              "endTimeUnixNano": str(start + int(dur_ms * 1e6)),
              "attributes": _kv({"gen_ai.operation.name": op, **attrs})}
        if error:
            sp["status"] = {"code": 2, "message": error}
        spans.append(sp)
        return start + int(dur_ms * 1e6)

    def chat(out_msgs, in_tok, out_tok, error=None):
        nonlocal t
        dur = r.uniform(400, 2400) * (1.6 if s["model"] == MODELS[1] else 1)
        t = span(f"chat {s['model']}", "chat", t, dur, {
            "gen_ai.provider.name": "openai" if s["model"].startswith("gpt") else "anthropic",
            "gen_ai.request.model": s["model"], "gen_ai.response.model": s["model"],
            "gen_ai.usage.input_tokens": in_tok, "gen_ai.usage.output_tokens": 0 if error else out_tok,
            "gen_ai.input.messages": json.dumps(history),
            "gen_ai.output.messages": None if error else json.dumps(out_msgs),
            "error.type": "429" if error else None}, error)

    def tool(name, args, result, error=None):
        nonlocal t
        cid = "call_" + _id(trace, name, len(spans), n=10)
        chat([_call(name, args, cid)], 600 + 90 * len(history), r.randint(20, 60))
        t = span(f"execute_tool {name}", "execute_tool", t, r.uniform(60, 900), {
            "gen_ai.tool.name": name, "gen_ai.tool.call.id": cid, "gen_ai.tool.call.arguments": json.dumps(args),
            "gen_ai.tool.call.result": json.dumps(result)}, error)
        history.extend([_call(name, args, cid), _tool_reply(cid, result)])

    fate = r.random()
    order = r.randint(4000, 4999)
    if fate < 0.03:   # the model provider rate-limits the first call: the customer gets nothing
        chat([], 600, 0, error="429 Too Many Requests: rate limit reached for requests")
        reply = None
    elif s["kind"] == "return":
        tool("getOrderDetails", {"order_id": order}, {"status": "delivered", "eligible_for_return": True})
        if fate < 0.13:   # the tool fails, the agent says it worked anyway
            tool("initiateReturn", {"order_id": order, "reason": "damaged"}, {"error": "DeadlockDetected"},
                 error="DeadlockDetected: transaction rolled back")
        else:
            tool("initiateReturn", {"order_id": order, "reason": "damaged"}, {"return_id": f"R-{order}", "label": "emailed"})
        reply = "Your return has been filed. You'll get a prepaid label by email within the hour."
    elif s["kind"] == "stock":
        if fate < 0.2:
            tool("checkInventory", {"product": s["product"]}, {"error": "product_not_found"},
                 error="product_not_found: sku lookup failed")
            reply = "Yes, it's in stock! You can order two today."
        else:
            tool("checkInventory", {"product": s["product"]}, {"in_stock": 14})
            reply = "Yes, we have 14 in stock. Want me to add two to your cart?"
    elif s["kind"] == "recommend":
        loops = 7 if fate < 0.12 else 1
        for i in range(loops):   # a loop: the same search again and again
            tool("searchProducts", {"query": "hiking gift", "max_price": 80, "page": 1}, {"results": [] if loops > 1 else PRODUCTS[:3]})
        reply = "How about a headlamp ($39), an insulated bottle ($29) or trekking poles ($69)?"
    else:
        tool("getOrderDetails", {"order_id": order}, {"status": "in_transit", "eta": "tomorrow"})
        reply = "It's on its way and should arrive tomorrow. Sorry for the delay!"
    if reply is not None:
        empty = r.random() < 0.025
        chat([] if empty else [_text("assistant", reply)], 700 + 120 * len(history), 0 if empty else r.randint(40, 140))
    total_in = sum(int(a["value"]["intValue"]) for sp in spans for a in sp["attributes"] if a["key"] == "gen_ai.usage.input_tokens")
    spans.insert(0, {"traceId": trace, "spanId": root, "name": f"invoke_agent {AGENT}", "kind": 2,
                     "startTimeUnixNano": str(t0 - 5 * 10**6), "endTimeUnixNano": str(t + 5 * 10**6),
                     "attributes": _kv({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": AGENT,
                                        "gen_ai.conversation.id": s["id"], "user.id": s["user"],
                                        "gen_ai.request.model": s["model"], "agent.turn": turn + 1,
                                        "agent.input_tokens_total": total_in})})
    return spans


def resource_spans(minute):
    """-> the minute's agent traces as resourceSpans (one entry), or [] when no turn falls in it."""
    out = []
    w = minute // WINDOW_MIN
    for window in (w - 1, w):            # sessions that started in this window or the one before
        for k in range(SESSIONS_PER_WINDOW):
            s = _session(window, k)
            turn = minute - s["start"]
            if 0 <= turn < s["turns"]:
                out += _turn(s, turn, minute)
    if not out:
        return []
    res = _kv({"service.name": SERVICE, "service.version": "2.3.0", "service.namespace": "shop",
               "deployment.environment": "production", "telemetry.sdk.language": "python",
               "telemetry.sdk.name": "opentelemetry"})
    return [{"resource": {"attributes": res}, "scopeSpans": [{"scope": {"name": "opentelemetry.instrumentation.openai_v2"},
                                                               "spans": out}]}]
