// AI agents (lib/agents.ts, lib/llmPrices.ts): sessions from the aggregate groups, issues without AI,
// and transcripts from each OpenTelemetry GenAI convention.
import { test } from "node:test";
import assert from "node:assert/strict";
import { findIssues, issueTag, mergeSessions, transcript } from "./lib/agents";
import { estimateCost, llmPrice } from "./lib/llmPrices";

const g = (o: Record<string, unknown>) => ({ count: 1, "min(ts_unix_nano)": 1e15, "max(ts_unix_nano)": 2e15, "max(duration_ns)": 0,
  "sum(gen_ai_input_tokens)": null, "sum(gen_ai_output_tokens)": null, "sum(gen_ai_cost)": null, status_code: 0, ...o });

test("a trace's groups join its session even without the session ID; cost reported or estimated", () => {
  const s = mergeSessions([
    g({ trace_id: "t1", session_id: "s1", gen_ai_operation: "invoke_agent", gen_ai_agent: "support" }),
    g({ trace_id: "t1", session_id: null, gen_ai_operation: "chat", gen_ai_model: "gpt-4o-mini", count: 2,
        "sum(gen_ai_input_tokens)": 1_000_000, "sum(gen_ai_output_tokens)": 100_000 }),
    g({ trace_id: "t1", session_id: null, gen_ai_operation: "execute_tool", count: 3 }),
    g({ trace_id: "t1", session_id: null, gen_ai_operation: "execute_tool", status_code: 2 }),
    g({ trace_id: "t2", session_id: "s1", gen_ai_operation: "chat", gen_ai_model: "claude-sonnet-4", "max(ts_unix_nano)": 3e15,
        "sum(gen_ai_input_tokens)": 10, "sum(gen_ai_output_tokens)": 5, "sum(gen_ai_cost)": 0.5 }),
    g({ trace_id: "t3", session_id: null, gen_ai_operation: "chat", gen_ai_model: "my-own-model", "sum(gen_ai_input_tokens)": 7 }),
  ]);
  assert.deepEqual(s.map((x) => x.id), ["s1", "t3"]);           // newest first; t3 has no session: its own
  const [s1, t3] = s;
  assert.deepEqual(s1.traces, ["t1", "t2"]);
  assert.equal(s1.agent, "support");
  assert.equal(s1.llmCalls, 3);
  assert.equal(s1.toolCalls, 4);
  assert.equal(s1.toolErrors, 1);
  assert.equal(s1.input, 1_000_010);
  assert.equal(s1.cost, 0.5);                                   // reported by the SDK: used as is
  assert.equal(s1.costEstimated, false);
  assert.equal(t3.single, true);
  assert.equal(t3.cost, null);                                  // an unknown model: tokens, no price
  const est = mergeSessions([g({ trace_id: "t", session_id: "s", gen_ai_operation: "chat", gen_ai_model: "gpt-4o-mini",
                                 "sum(gen_ai_input_tokens)": 1_000_000, "sum(gen_ai_output_tokens)": 1_000_000 })])[0];
  assert.ok(Math.abs(est.cost! - 0.75) < 1e-9 && est.costEstimated);   // $0.15 in + $0.60 out per million
});

test("list prices match by model prefix, ignoring providers and versions", () => {
  assert.deepEqual(llmPrice("gpt-4o-mini-2024-07-18"), { input: 0.15, output: 0.6 });
  assert.deepEqual(llmPrice("gpt-4o-2024-08-06"), { input: 2.5, output: 10 });   // not gpt-4o-mini
  assert.deepEqual(llmPrice("us.anthropic.claude-sonnet-4-20250514-v1:0"), { input: 3, output: 15 });
  assert.deepEqual(llmPrice("openai/gpt-4.1-mini"), { input: 0.4, output: 1.6 });
  assert.equal(llmPrice("llama-3-70b"), null);
  assert.equal(estimateCost(undefined, 1, 1), null);
});

test("issues: a tool error is hidden when the agent run still ended OK; loops from 5 calls", () => {
  const sessions = mergeSessions([g({ trace_id: "t1", session_id: "s1", gen_ai_operation: "invoke_agent" }),
                                  g({ trace_id: "t2", session_id: "s2", gen_ai_operation: "invoke_agent" })]);
  const issues = findIssues(sessions, [
    { gen_ai_operation: "execute_tool", gen_ai_tool: "initiateReturn", status_message: "DeadlockDetected", trace_id: "t1", count: 1 },
    { gen_ai_operation: "execute_tool", gen_ai_tool: "initiateReturn", status_message: "DeadlockDetected", trace_id: "t2", count: 1 },
    { gen_ai_operation: "invoke_agent", trace_id: "t2", count: 1 },
    { gen_ai_operation: "chat", gen_ai_model: "gpt-4o", status_message: "429", trace_id: "t2", count: 2 },
  ], [{ gen_ai_model: "gpt-4o", trace_id: "t1", count: 1 }],
     [{ trace_id: "t1", gen_ai_tool: "search", count: 7 }, { trace_id: "t2", gen_ai_tool: "search", count: 4 }]);
  const by = Object.fromEntries(issues.map((i) => [i.key, i]));
  assert.deepEqual(by["tool_hidden:initiateReturn"].sessions, ["s1"]);   // t1's run didn't fail: the customer was told it worked
  assert.equal(by["tool_hidden:initiateReturn"].example, "DeadlockDetected");
  assert.deepEqual(by["tool_error:initiateReturn"].sessions, ["s2"]);
  assert.equal(by["llm_error:gpt-4o"].occurrences, 2);
  assert.deepEqual(by["empty_reply:gpt-4o"].sessions, ["s1"]);
  assert.deepEqual(by["loop:search"].sessions, ["s1"]);               // 4 calls isn't a loop
  assert.equal(issueTag(by["tool_hidden:initiateReturn"]), "initiateReturn failed silently");
});

const span = (o: Record<string, unknown>) => ({ trace_id: "t1", status_code: 0, duration_ns: 5e6, events: [], ...o });

test("transcript: current convention, history shown once, replies always, tools and errors", () => {
  const sys = { role: "system", parts: [{ type: "text", content: "Be nice." }] };
  const u1 = { role: "user", parts: [{ type: "text", content: "Where is #4471?" }] };
  const call = { role: "assistant", parts: [{ type: "tool_call", name: "getOrder", arguments: { id: 4471 } }] };
  const reply = { role: "assistant", parts: [{ type: "text", content: "On its way." }] };
  const e = transcript([
    span({ ts_unix_nano: 3, gen_ai_operation: "chat", gen_ai_model: "gpt-4o", gen_ai_output_tokens: 5,
           attributes: { "gen_ai.input.messages": JSON.stringify([sys, u1, call]), "gen_ai.output.messages": JSON.stringify([reply]) } }),
    span({ ts_unix_nano: 1, gen_ai_operation: "chat", gen_ai_model: "gpt-4o", gen_ai_output_tokens: 9,
           attributes: { "gen_ai.input.messages": JSON.stringify([sys, u1]), "gen_ai.output.messages": JSON.stringify([call]) } }),
    span({ ts_unix_nano: 2, gen_ai_operation: "execute_tool", gen_ai_tool: "getOrder", status_code: 2, status_message: "timeout",
           attributes: { "gen_ai.tool.call.arguments": '{"id":4471}', "gen_ai.tool.call.result": "" } }),
    span({ ts_unix_nano: 4, gen_ai_operation: "chat", gen_ai_model: "gpt-4o", gen_ai_output_tokens: 5,   // the same reply again: shown
           attributes: { "gen_ai.input.messages": JSON.stringify([sys, u1]), "gen_ai.output.messages": JSON.stringify([reply]) } }),
    span({ ts_unix_nano: 5, gen_ai_operation: "chat", gen_ai_model: "gpt-4o", gen_ai_output_tokens: 0, attributes: {} }),
  ]);
  assert.deepEqual(e.map((x) => x.kind === "message" ? `${x.role}: ${x.text}` : x.kind === "tool" ? `tool ${x.name} ${x.args} ${x.error}` : `error ${x.what}`), [
    "system: Be nice.", "user: Where is #4471?", 'tool getOrder {"id":4471} timeout', "assistant: On its way.", "assistant: On its way.",
    "error Empty reply from gpt-4o",
  ]);
});

test("transcript: older conventions (events, OpenLLMetry's indexed attributes)", () => {
  const e = transcript([
    span({ ts_unix_nano: 1, gen_ai_operation: "chat", attributes: {}, events: [
      { name: "gen_ai.user.message", attributes: { content: "hi" } },
      { name: "gen_ai.choice", attributes: { "gen_ai.event.content": JSON.stringify({ message: { content: "hello" } }) } }] }),
    span({ ts_unix_nano: 2, gen_ai_operation: "chat", attributes: {
      "gen_ai.prompt.0.role": "user", "gen_ai.prompt.0.content": "and you?", "gen_ai.completion.0.content": "fine" } }),
  ]);
  assert.deepEqual(e.map((x) => x.kind === "message" ? `${x.role}: ${x.text}` : x.kind), [
    "user: hi", 'assistant: {"content":"hello"}', "user: and you?", "assistant: fine"]);
});
