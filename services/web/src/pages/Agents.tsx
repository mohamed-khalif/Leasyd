// AI agents (docs/agents-design.md): sessions, silent failures and one session's conversation, from
// OpenTelemetry GenAI spans. Lists and issues are aggregate queries over the agent columns only
// (lib/agents.ts); a session's transcript reads its traces' spans in full.
import { useMemo, useState } from "react";
import { fmtAgo, fmtCost, fmtDateTime, fmtDuration, fmtNum, fmtPercent } from "../lib/format";
import { Entry, errorsQuery, emptyQuery, findIssues, Issue, issueTag, loopsQuery, mergeSessions, Session, SESSION_ROWS,
         sessionLookup, sessionsQuery, TEXT_MAX, traceSpans, transcript } from "../lib/agents";
import { query, records, Result } from "../api";
import type { Ctx } from "../App";
import { Card, Drawer, Tabs } from "../components/Charts";
import { Loads, Panel } from "../components/Panel";
import { useHashParam } from "../hashParams";
import { useLoad, useQuery } from "../useQuery";
import { useRangeWindow } from "../useNow";

const rows = (r: Result | null) => (r ? records(r) : []);
const MAX_TRACES = 20;      // turns read for one transcript
const PAGE = 200;
type Tab = "sessions" | "issues";

export function Agents({ ctx }: { ctx: Ctx }) {
  const w = useRangeWindow(ctx);
  const key = ctx.range.key + ctx.tick;
  const list = useQuery(sessionsQuery(w), "s" + key);
  const errors = useQuery(errorsQuery(w), "e" + key);
  const empty = useQuery(emptyQuery(w), "m" + key);
  const loops = useQuery(loopsQuery(w), "l" + key);
  const [tab, setTab] = useHashParam("tab", "sessions");
  const [open, setOpen] = useHashParam("session");

  const sessions = useMemo(() => mergeSessions(rows(list.data)), [list.data]);
  const issues = useMemo(() => findIssues(sessions, rows(errors.data), rows(empty.data), rows(loops.data)),
                         [sessions, errors.data, empty.data, loops.data]);
  const flagged = useMemo(() => {
    const m = new Map<string, Issue[]>();
    for (const i of issues) for (const s of i.sessions) m.set(s, [...(m.get(s) ?? []), i]);
    return m;
  }, [issues]);
  const totals = useMemo(() => sessions.reduce((t, s) => ({
    runs: t.runs + s.traces.length, llm: t.llm + s.llmCalls, input: t.input + s.input, output: t.output + s.output,
    cost: t.cost + (s.cost ?? 0), estimated: t.estimated || s.costEstimated,
  }), { runs: 0, llm: 0, input: 0, output: 0, cost: 0, estimated: false }), [sessions]);
  const selected = sessions.find((s) => s.id === open);

  if (list.data && !sessions.length) return <NoAgents />;
  const withIssues = sessions.filter((s) => flagged.has(s.id)).length;
  return (
    <div className="agents">
      <div className="cards" style={{ gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
        <Card label="Sessions" value={list.data ? fmtNum(sessions.length) : "…"} sub={`${fmtNum(totals.runs)} agent runs`} />
        <Card label="LLM calls" value={list.data ? fmtNum(totals.llm) : "…"}
              sub={`${fmtNum(totals.input)} tokens in · ${fmtNum(totals.output)} out`} />
        <Card label={totals.estimated ? "Cost (estimated)" : "Cost"} value={list.data ? `${totals.estimated ? "≈ " : ""}${fmtCost(totals.cost)}` : "…"}
              sub={sessions.length ? `${fmtCost(totals.cost / sessions.length)} a session` : "–"} />
        <Card label="Sessions with issues" value={list.data && errors.data ? fmtNum(withIssues) : "…"}
              sub={sessions.length ? `${fmtPercent((withIssues / sessions.length) * 100)} of sessions` : "–"}
              tone={withIssues ? "bad" : "ok"} onClick={() => setTab("issues")} />
        <Card label="Issues" value={errors.data ? fmtNum(issues.length) : "…"} sub="found in the traces, no setup"
              tone={issues.length ? "warn" : "ok"} onClick={() => setTab("issues")} />
      </div>
      <Tabs<Tab> tabs={[["sessions", "Sessions"], ["issues", `Issues${issues.length ? ` (${issues.length})` : ""}`]]}
                 active={tab === "issues" ? "issues" : "sessions"} onPick={setTab} />
      {tab === "issues"
        ? <IssuesView q={errors} issues={issues} total={sessions.length} onOpen={setOpen} />
        : <SessionsView q={list} sessions={sessions} flagged={flagged} onOpen={setOpen} />}
      {open && <SessionDrawer ctx={ctx} w={w} id={open} session={selected} issues={flagged.get(open) ?? []} onClose={() => setOpen("")} />}
    </div>
  );
}

function NoAgents() {
  return (
    <Panel title="AI agents">
      <div className="state" style={{ display: "grid", gap: 8, justifyItems: "start", textAlign: "left", padding: 20 }}>
        <b>No AI agent traces in this time range.</b>
        <span className="muted">Leasyd shows agent sessions, conversations, tokens, cost and silent failures from OpenTelemetry's
          GenAI spans (<code className="mono">gen_ai.*</code>): the OpenTelemetry instrumentations for OpenAI, Anthropic, Bedrock
          and Vertex, OpenLIT, OpenLLMetry, the Vercel AI SDK and Pydantic AI send them. Group a conversation's runs with
          <code className="mono"> gen_ai.conversation.id</code> (or <code className="mono">session.id</code>).</span>
        <a href="#/start?source=other">Send OpenTelemetry data to Leasyd</a>
      </div>
    </Panel>
  );
}

// ------------------------------------------------------------------ sessions

function SessionsView({ q, sessions, flagged, onOpen }: { q: { data: Result | null; error: string | null; loading: boolean };
  sessions: Session[]; flagged: Map<string, Issue[]>; onOpen: (id: string) => void }) {
  const [filter, setFilter] = useState("");
  const [problems, setProblems] = useState(false);
  const [shown, setShown] = useState(PAGE);
  const f = filter.trim().toLowerCase();
  const list = sessions.filter((s) => (!problems || s.errors || flagged.has(s.id))
    && (!f || s.id.toLowerCase().includes(f) || s.agent.toLowerCase().includes(f) || Object.keys(s.models).some((m) => m.toLowerCase().includes(f))));
  return (
    <>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <input className="input grow" style={{ flex: 1 }} placeholder="Search by session ID, agent or model…" value={filter}
               onChange={(e) => { setFilter(e.target.value); setShown(PAGE); }} aria-label="Search sessions" />
        <label className="faint" style={{ display: "flex", gap: 6, alignItems: "center", whiteSpace: "nowrap" }}>
          <input type="checkbox" checked={problems} onChange={(e) => setProblems(e.target.checked)} /> Only sessions with problems
        </label>
      </div>
      <Panel title="Sessions" flush right={q.data?.rows.length === SESSION_ROWS ? <span className="faint">newest runs in the range</span> : undefined}>
        <Loads q={q} empty={!list.length} height={160}>
          {() => (
            <>
              <table className="dtable">
                <thead><tr><th style={{ width: "21%" }}>Session</th><th style={{ width: "13%" }}>Agent</th><th style={{ width: "9%" }}>Last activity</th>
                  <th className="num" style={{ width: "5%" }}>Turns</th><th className="num" style={{ width: "6%" }}>LLM calls</th>
                  <th className="num" style={{ width: "5%" }}>Tools</th><th className="num" style={{ width: "7%" }}>Tokens</th>
                  <th className="num" style={{ width: "8%" }} title="Estimated from list prices when the SDK doesn't report it">Cost ≈</th>
                  <th className="num" style={{ width: "7%" }}>Duration</th><th>Problems</th></tr></thead>
                <tbody>
                  {list.slice(0, shown).map((s) => {
                    const its = flagged.get(s.id) ?? [];
                    return (
                      <tr key={s.id} onClick={() => onOpen(s.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onOpen(s.id)}>
                        <td title={s.id}>{s.single ? <span className="faint">run </span> : null}{s.id}</td>
                        <td>{s.agent || "–"}</td>
                        <td title={fmtDateTime(s.end, true)}>{fmtAgo(s.end)}</td>
                        <td className="num">{s.traces.length}</td>
                        <td className="num">{fmtNum(s.llmCalls)}</td>
                        <td className="num">{fmtNum(s.toolCalls)}</td>
                        <td className="num">{fmtNum(s.input + s.output)}</td>
                        <td className="num">{s.cost == null ? "–" : fmtCost(s.cost)}</td>
                        <td className="num">{fmtDuration(s.end - s.start)}</td>
                        <td>{its.length ? its.slice(0, 2).map((i) => <span key={i.key} className={`pill ${i.kind === "loop" || i.kind === "empty_reply" ? "warn" : "bad"}`}
                                style={{ marginRight: 4 }}>{issueTag(i)}</span>)
                          : s.errors ? <span className="pill bad">{fmtNum(s.errors)} errors</span> : <span className="faint">–</span>}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {list.length > shown && <div className="state" style={{ minHeight: 0, padding: 10 }}>
                <button className="btn" onClick={() => setShown(shown + PAGE)}>Show {fmtNum(Math.min(PAGE, list.length - shown))} more of {fmtNum(list.length - shown)}</button></div>}
            </>
          )}
        </Loads>
      </Panel>
    </>
  );
}

// ------------------------------------------------------------------ issues

function IssuesView({ q, issues, total, onOpen }: { q: { data: Result | null; error: string | null; loading: boolean };
  issues: Issue[]; total: number; onOpen: (id: string) => void }) {
  return (
    <Panel title="Issues" right={<span className="faint">found in every agent trace of the range: tool and LLM errors, empty replies, loops</span>}>
      <Loads q={q} empty={!issues.length} height={120}>
        {() => (
          <div className="agent-issues">
            {issues.map((i) => (
              <div key={i.key} className={`agent-issue ${i.kind === "loop" || i.kind === "empty_reply" ? "warn" : "bad"}`}>
                <div className="agent-issue-head">
                  <b>{i.title}</b>
                  <span className="faint">{fmtNum(i.sessions.length)} of {fmtNum(total)} sessions · {i.occurrences === 1 ? "once" : `${fmtNum(i.occurrences)} times`}</span>
                </div>
                <div className="muted">{i.detail}{i.example ? <> Error: <code className="mono">{i.example}</code></> : null}</div>
                <div className="agent-issue-examples">
                  <span className="faint">Examples:</span>
                  {i.sessions.slice(0, 5).map((s) => <button key={s} className="btn small" onClick={() => onOpen(s)} title={s}>{s.slice(0, 13)}</button>)}
                </div>
              </div>
            ))}
          </div>
        )}
      </Loads>
    </Panel>
  );
}

// ------------------------------------------------------------------ one session

function SessionDrawer({ ctx, w, id, session, issues, onClose }: { ctx: Ctx; w: { start: string; end: string }; id: string;
  session?: Session; issues: Issue[]; onClose: () => void }) {
  // The session's traces: from the list, or (opened from a link) found by its ID, bloom-pruned.
  const spans = useLoad(async (signal) => {
    let traces = session?.traces;
    if (!traces) traces = [...new Set(rows(await query(sessionLookup(w, id), { signal })).map((r) => String(r.trace_id)))];
    const parts = await Promise.all(traces.slice(0, MAX_TRACES).map((t) => query(traceSpans(w, t), { signal })));
    return { traces, spans: parts.flatMap(rows) };
  }, id + ctx.range.key + ctx.tick);
  const entries = useMemo(() => transcript(spans.data?.spans ?? []), [spans.data]);
  const fromSpans = useMemo(() => (spans.data && !session ? mergeSessions(spans.data.spans.filter((r) => r.gen_ai_operation).map((r) => ({
    ...r, count: 1, "min(ts_unix_nano)": r.ts_unix_nano, "max(ts_unix_nano)": r.ts_unix_nano, "max(duration_ns)": r.duration_ns,
    "sum(gen_ai_input_tokens)": r.gen_ai_input_tokens, "sum(gen_ai_output_tokens)": r.gen_ai_output_tokens, "sum(gen_ai_cost)": r.gen_ai_cost,
    session_id: r.session_id ?? null })))[0] : undefined), [spans.data, session]);
  const s = session ?? fromSpans;
  return (
    <Drawer title={<>Session <span className="mono">{id}</span></>} onClose={onClose}>
      {s && (
        <div className="drawer-section">
          <div className="cards" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
            <Card label="Turns" value={fmtNum(s.traces.length)} sub={s.agent || "agent"} />
            <Card label="LLM calls" value={fmtNum(s.llmCalls)} sub={`${fmtNum(s.toolCalls)} tool calls`} />
            <Card label="Tokens" value={fmtNum(s.input + s.output)} sub={`${fmtNum(s.input)} in · ${fmtNum(s.output)} out`} />
            <Card label={s.costEstimated ? "Cost (estimated)" : "Cost"} value={s.cost == null ? "–" : fmtCost(s.cost)}
                  sub={s.costEstimated ? "from list prices" : s.cost == null ? "unknown model" : "reported by the SDK"} />
          </div>
          <div className="faint" style={{ marginTop: 8 }}>
            {fmtDateTime(s.start, true)} · {fmtDuration(s.end - s.start)} ·{" "}
            {Object.entries(s.models).map(([m, u]) => `${m}: ${fmtNum(u.calls)} calls, ${fmtNum(u.input + u.output)} tokens`).join(" · ") || "no LLM calls"}
          </div>
          {issues.length > 0 && <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
            {issues.map((i) => <span key={i.key} className={`pill ${i.kind === "loop" || i.kind === "empty_reply" ? "warn" : "bad"}`}>{i.title}</span>)}
          </div>}
        </div>
      )}
      <div className="drawer-section">
        <h4>Conversation</h4>
        <Loads q={spans} empty={!entries.length} height={200}>
          {() => (
            <div className="agent-transcript">
              {entries.map((e, i) => <TranscriptEntry key={i} e={e} />)}
              {(spans.data?.traces.length ?? 0) > MAX_TRACES && <div className="faint">Showing the first {MAX_TRACES} of {spans.data!.traces.length} turns.</div>}
            </div>
          )}
        </Loads>
      </div>
    </Drawer>
  );
}

function TranscriptEntry({ e }: { e: Entry }) {
  const [all, setAll] = useState(false);
  const trace = <a className="faint agent-trace" href={`#/traces/${e.trace}`} title="Open the trace">trace ↗</a>;
  if (e.kind === "tool") {
    return (
      <div className={`agent-msg tool${e.error ? " failed" : ""}`}>
        <div className="agent-msg-head"><b>Tool · {e.name}</b><span className="faint">{fmtDuration(e.ms)}</span>{trace}</div>
        {e.args && <div className="mono agent-code"><span className="faint">args </span>{e.args}</div>}
        {e.result && <div className="mono agent-code"><span className="faint">result </span>{e.result}</div>}
        {e.error && <div className="agent-err">Failed: {e.error}</div>}
      </div>
    );
  }
  if (e.kind === "error") {
    return <div className="agent-msg failed"><div className="agent-msg-head"><b>{e.what}</b>{trace}</div><div className="agent-err">{e.message}</div></div>;
  }
  const cut = e.role === "system" ? 300 : TEXT_MAX;   // the system prompt repeats every session: short until asked
  return (
    <div className={`agent-msg ${e.role}`}>
      <div className="agent-msg-head"><b>{e.role === "assistant" ? "Assistant" : e.role === "user" ? "User" : e.role === "system" ? "System" : e.role}</b>
        {e.model && <span className="faint">{e.model}</span>}{trace}</div>
      <div className="agent-text">{all ? e.text : e.text.slice(0, cut)}
        {!all && e.text.length > cut && <>… <button className="btn small" onClick={() => setAll(true)}>show all</button></>}
      </div>
    </div>
  );
}

