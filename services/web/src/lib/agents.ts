// AI agents (docs/agents-design.md): the Agents page's queries and what it makes of their answers.
// Every list and issue reads only the agent columns compaction lifts out of the attribute map
// (session_id, gen_ai_operation, gen_ai_model, gen_ai_agent, gen_ai_tool, tokens, cost), never the
// prompts. A transcript reads whole spans, one session's traces only.
import type { Query } from "../api";
import { estimateCost } from "./llmPrices";

export const CHAT_OPS = ["chat", "text_completion", "generate_content"];
export const LOOP_CALLS = 5;              // the same tool this often in one trace is a loop
export const SESSION_ROWS = 5000;         // groups the list query returns: the newest traces first
export const TEXT_MAX = 4000;   // characters of a message shown before "show all"

type Row = Record<string, unknown>;
type W = { start: string; end: string };
const num = (v: unknown) => (v == null || v === "" ? 0 : Number(v));
const str = (v: unknown) => (v == null ? "" : String(v));
const AGENT = { field: "gen_ai_operation", op: "exists" };

// ------------------------------------------------------------------ queries (aggregates over the agent columns)

/** Per trace (and session, operation, model, agent, status): a trace's spans without the session ID
 *  still count towards its session, since the page merges a trace's groups. Newest traces first. */
export const sessionsQuery = (w: W): Query => ({
  signal: "traces", ...w, where: [AGENT],
  group_by: ["trace_id", "session_id", "gen_ai_operation", "gen_ai_model", "gen_ai_agent", "status_code"],
  aggs: [{ fn: "max", field: "ts_unix_nano" }, { fn: "min", field: "ts_unix_nano" }, { fn: "count" },
         { fn: "sum", field: "gen_ai_input_tokens" }, { fn: "sum", field: "gen_ai_output_tokens" }, { fn: "sum", field: "gen_ai_cost" },
         { fn: "max", field: "duration_ns" }],
  order: "desc", limit: SESSION_ROWS,
});

/** Failed agent spans, by what failed and where (rare: few groups). */
export const errorsQuery = (w: W): Query => ({
  signal: "traces", ...w, where: [AGENT, { field: "status_code", op: "=", value: 2 }],
  group_by: ["gen_ai_operation", "gen_ai_tool", "gen_ai_model", "status_message", "trace_id", "session_id"],
  aggs: [{ fn: "count" }], limit: SESSION_ROWS,
});

/** LLM calls that answered with nothing (0 output tokens, not an error). */
export const emptyQuery = (w: W): Query => ({
  signal: "traces", ...w,
  where: [{ field: "gen_ai_operation", op: "in", value: CHAT_OPS }, { field: "gen_ai_output_tokens", op: "=", value: 0 },
          { field: "status_code", op: "!=", value: 2 }],
  group_by: ["gen_ai_model", "trace_id", "session_id"], aggs: [{ fn: "count" }], limit: SESSION_ROWS,
});

/** Tool calls per trace and tool, most repeated first: the page keeps LOOP_CALLS or more. */
export const loopsQuery = (w: W): Query => ({
  signal: "traces", ...w, where: [{ field: "gen_ai_operation", op: "=", value: "execute_tool" }],
  group_by: ["trace_id", "session_id", "gen_ai_tool"], aggs: [{ fn: "count" }], order: "desc", limit: 500,
});

/** A session's own spans (the ones carrying its ID: their traces, when it isn't in the list). */
export const sessionLookup = (w: W, id: string): Query => ({ signal: "traces", ...w, match: { session_id: id }, search: { limit: 200 } });

/** One trace's spans, in full (messages, tool arguments and results): bloom-pruned by trace ID. */
export const traceSpans = (w: W, trace: string): Query => ({ signal: "traces", ...w, match: { trace_id: trace }, search: { limit: 1000 } });

// ------------------------------------------------------------------ sessions

export type Usage = { input: number; output: number; calls: number };
export type Session = {
  id: string; single: boolean;            // single: a trace with no session ID, shown as its own session
  traces: string[]; start: number; end: number;   // ms
  agent: string; models: Record<string, Usage>;
  input: number; output: number; llmCalls: number; toolCalls: number;
  errors: number; toolErrors: number; llmErrors: number; agentErrors: number;
  cost: number | null; costEstimated: boolean;     // reported (gen_ai.usage.cost) or estimated from list prices
};

/** The sessions query's groups -> sessions, newest first. */
export function mergeSessions(rows: Row[]): Session[] {
  const sessionOf = new Map<string, string>();
  for (const r of rows) if (r.session_id) sessionOf.set(str(r.trace_id), str(r.session_id));
  const by = new Map<string, Session & { reported: number }>();
  for (const r of rows) {
    const trace = str(r.trace_id), id = sessionOf.get(trace) ?? trace;
    let s = by.get(id);
    if (!s) {
      s = { id, single: !sessionOf.has(trace), traces: [], start: Infinity, end: 0, agent: "", models: {}, input: 0, output: 0,
            llmCalls: 0, toolCalls: 0, errors: 0, toolErrors: 0, llmErrors: 0, agentErrors: 0, cost: null, costEstimated: false, reported: 0 };
      by.set(id, s);
    }
    if (!s.traces.includes(trace)) s.traces.push(trace);
    const n = num(r.count), op = str(r.gen_ai_operation), err = num(r.status_code) === 2;
    s.start = Math.min(s.start, num(r["min(ts_unix_nano)"]) / 1e6);
    s.end = Math.max(s.end, (num(r["max(ts_unix_nano)"]) + num(r["max(duration_ns)"])) / 1e6);
    if (r.gen_ai_agent && !s.agent) s.agent = str(r.gen_ai_agent);
    s.input += num(r["sum(gen_ai_input_tokens)"]);
    s.output += num(r["sum(gen_ai_output_tokens)"]);
    s.reported += num(r["sum(gen_ai_cost)"]);
    if (CHAT_OPS.includes(op)) {
      s.llmCalls += n;
      const m = str(r.gen_ai_model) || "unknown model", u = (s.models[m] ??= { input: 0, output: 0, calls: 0 });
      u.input += num(r["sum(gen_ai_input_tokens)"]); u.output += num(r["sum(gen_ai_output_tokens)"]); u.calls += n;
      if (err) s.llmErrors += n;
    } else if (op === "execute_tool") {
      s.toolCalls += n;
      if (err) s.toolErrors += n;
    } else if (err) s.agentErrors += n;
    if (err) s.errors += n;
  }
  return [...by.values()].map(({ reported, ...s }) => {
    if (reported > 0) return { ...s, cost: reported };
    const est = Object.entries(s.models).map(([m, u]) => estimateCost(m, u.input, u.output));
    const known = est.filter((c): c is number => c != null);
    return { ...s, cost: known.length ? known.reduce((a, b) => a + b, 0) : null, costEstimated: known.length > 0 };
  }).sort((a, b) => b.end - a.end);
}

// ------------------------------------------------------------------ issues (no AI: from the columns)

export type IssueKind = "tool_hidden" | "tool_error" | "llm_error" | "empty_reply" | "loop";
/** A short name for a session's issue (its problems column). */
export const issueTag = (i: { kind: IssueKind; subject: string }) => ({
  tool_hidden: `${i.subject} failed silently`, tool_error: `${i.subject} failed`, llm_error: "LLM error",
  empty_reply: "empty reply", loop: `${i.subject} loop`,
}[i.kind]);
export type Issue = { key: string; kind: IssueKind; subject: string; title: string; detail: string;
                      sessions: string[]; occurrences: number; example: string };
const TITLES: Record<IssueKind, (s: string) => string> = {
  tool_hidden: (s) => `${s} failed, and the agent answered anyway`,
  tool_error: (s) => `${s} failed, and the run failed`,
  llm_error: (s) => `LLM calls to ${s} failed`,
  empty_reply: (s) => `${s} returned an empty reply`,
  loop: (s) => `${s} called ${LOOP_CALLS}+ times in one run`,
};
const DETAILS: Record<IssueKind, string> = {
  tool_hidden: "The tool call errored but the agent run ended OK: the customer may have been told it worked.",
  tool_error: "The tool call errored and so did the agent run.",
  llm_error: "The model call errored (rate limit, timeout, bad request); the turn got no answer from it.",
  empty_reply: "The model answered with 0 output tokens.",
  loop: "The agent kept calling the same tool in one run: it may be stuck.",
};
/** Issues from the errors, empty-reply and loops queries, most sessions first. A tool error is
 *  "hidden" when its trace's agent run (invoke_agent) did not fail. */
export function findIssues(sessions: Session[], errors: Row[], empty: Row[], loops: Row[]): Issue[] {
  const sessionOfTrace = new Map<string, string>();
  for (const s of sessions) for (const t of s.traces) sessionOfTrace.set(t, s.id);
  const failedRuns = new Set(errors.filter((r) => str(r.gen_ai_operation) === "invoke_agent").map((r) => str(r.trace_id)));
  const by = new Map<string, Issue>();
  const add = (kind: IssueKind, subject: string, trace: string, session: unknown, n: number, example = "") => {
    const key = `${kind}:${subject}`, sid = sessionOfTrace.get(trace) ?? (str(session) || trace);
    const it = by.get(key) ?? { key, kind, subject, title: TITLES[kind](subject), detail: DETAILS[kind], sessions: [], occurrences: 0, example };
    if (!it.sessions.includes(sid)) it.sessions.push(sid);
    it.occurrences += n;
    if (!it.example && example) it.example = example;
    by.set(key, it);
  };
  for (const r of errors) {
    const op = str(r.gen_ai_operation), trace = str(r.trace_id);
    if (op === "execute_tool") add(failedRuns.has(trace) ? "tool_error" : "tool_hidden", str(r.gen_ai_tool) || "a tool", trace, r.session_id, num(r.count), str(r.status_message));
    else if (CHAT_OPS.includes(op)) add("llm_error", str(r.gen_ai_model) || "the model", trace, r.session_id, num(r.count), str(r.status_message));
  }
  for (const r of empty) add("empty_reply", str(r.gen_ai_model) || "the model", str(r.trace_id), r.session_id, num(r.count));
  for (const r of loops) if (num(r.count) >= LOOP_CALLS) add("loop", str(r.gen_ai_tool) || "a tool", str(r.trace_id), r.session_id, num(r.count));
  const rank: Record<IssueKind, number> = { tool_hidden: 0, llm_error: 1, tool_error: 2, empty_reply: 3, loop: 4 };
  return [...by.values()].sort((a, b) => b.sessions.length - a.sessions.length || rank[a.kind] - rank[b.kind]);
}

// ------------------------------------------------------------------ transcript

export type Entry =
  | { kind: "message"; role: string; text: string; long: boolean; t: number; trace: string; model?: string }
  | { kind: "tool"; name: string; args: string; result: string; error: string; ms: number; t: number; trace: string }
  | { kind: "error"; what: string; message: string; t: number; trace: string };

const clip = (s: string) => ({ text: s, long: s.length > TEXT_MAX });
const parse = (v: unknown): unknown => { try { return typeof v === "string" ? JSON.parse(v) : v; } catch { return v; } };
const pretty = (v: unknown) => (v == null || v === "" ? "" : typeof v === "string" ? v : JSON.stringify(v));

/** A message's text: the current convention's parts (text, tool calls, tool responses), or a plain
 *  content field (older conventions). */
function messageText(m: Row): string {
  if (Array.isArray(m.parts)) {
    return (m.parts as Row[]).map((p) => p.type === "text" ? str(p.content)
      : p.type === "tool_call" ? `→ ${str(p.name)}(${pretty(parse(p.arguments))})`
      : p.type === "tool_call_response" ? `← ${pretty(parse(p.response))}` : pretty(p.content ?? p)).filter(Boolean).join("\n");
  }
  return pretty(parse(m.content ?? m.message ?? ""));
}

/** A chat span's input and output messages, from whichever convention its SDK used. */
function chatMessages(a: Row, events: Row[]): { input: Row[]; output: Row[] } {
  const inp = parse(a["gen_ai.input.messages"]), out = parse(a["gen_ai.output.messages"]);
  if (Array.isArray(inp) || Array.isArray(out)) return { input: (inp as Row[]) ?? [], output: (out as Row[]) ?? [] };
  const ev = events.filter((e) => str(e.name).startsWith("gen_ai."));
  if (ev.length) {   // older: one event per message
    const role = (n: string) => n.replace(/^gen_ai\./, "").replace(/\.message$/, "");
    const msg = (e: Row) => { const at = (e.attributes ?? {}) as Row, p = parse(at["gen_ai.event.content"]) as Row | null;
      return { role: role(str(e.name)), content: at.content ?? p?.content ?? p?.message ?? "" }; };
    return { input: ev.filter((e) => str(e.name) !== "gen_ai.choice").map(msg),
             output: ev.filter((e) => str(e.name) === "gen_ai.choice").map((e) => ({ ...msg(e), role: "assistant" })) };
  }
  const indexed = (prefix: string) => {   // OpenLLMetry: gen_ai.prompt.0.role / .content
    const out: Row[] = [];
    for (let i = 0; a[`${prefix}.${i}.content`] != null || a[`${prefix}.${i}.role`] != null; i++) {
      out.push({ role: a[`${prefix}.${i}.role`] ?? (prefix.endsWith("completion") ? "assistant" : "user"), content: a[`${prefix}.${i}.content`] ?? "" });
    }
    return out;
  };
  return { input: indexed("gen_ai.prompt"), output: indexed("gen_ai.completion") };
}

/** A session's spans (any order, from several traces) -> its conversation, oldest first. Each request
 *  repeats the history, so a message already shown isn't shown again. */
export function transcript(spans: Row[]): Entry[] {
  const out: Entry[] = [], seen = new Set<string>();
  const sorted = [...spans].sort((a, b) => num(a.ts_unix_nano) - num(b.ts_unix_nano));
  for (const sp of sorted) {
    const a = (sp.attributes ?? {}) as Row, op = str(sp.gen_ai_operation ?? a["gen_ai.operation.name"]);
    const t = num(sp.ts_unix_nano) / 1e6, trace = str(sp.trace_id), failed = num(sp.status_code) === 2;
    if (CHAT_OPS.includes(op)) {
      const { input, output } = chatMessages(a, (sp.events ?? []) as Row[]);
      const model = str(sp.gen_ai_model) || undefined;
      for (const [m, isOut] of [...input.map((m) => [m, false] as const), ...output.map((m) => [m, true] as const)]) {
        const role = str(m.role) || (isOut ? "assistant" : "user"), text = messageText(m);
        if (!text || role === "tool") continue;                 // tool results show with their tool call
        if (!isOut && role === "assistant" && text.startsWith("→ ")) continue;   // a tool call: shown as the tool
        if (isOut && text.startsWith("→ ")) continue;
        const key = `${role}\u0000${text}`;
        if (!isOut && seen.has(key)) continue;   // history the request repeats; a reply is always new
        seen.add(key);
        out.push({ kind: "message", role, ...clip(text), t, trace, model: isOut ? model : undefined });
      }
      if (failed) out.push({ kind: "error", what: `LLM call${model ? ` (${model})` : ""}`, message: str(sp.status_message), t, trace });
      else if (num(sp.gen_ai_output_tokens) === 0 && sp.gen_ai_output_tokens != null && !output.length) {
        out.push({ kind: "error", what: `Empty reply${model ? ` from ${model}` : ""}`, message: "0 output tokens", t, trace });
      }
    } else if (op === "execute_tool") {
      out.push({ kind: "tool", name: str(sp.gen_ai_tool ?? a["gen_ai.tool.name"]) || str(sp.name),
                 args: pretty(parse(a["gen_ai.tool.call.arguments"])), result: pretty(parse(a["gen_ai.tool.call.result"])),
                 error: failed ? str(sp.status_message) || "failed" : "", ms: num(sp.duration_ns) / 1e6, t, trace });
    } else if (failed) {
      out.push({ kind: "error", what: str(sp.name), message: str(sp.status_message), t, trace });
    }
  }
  return out;
}
