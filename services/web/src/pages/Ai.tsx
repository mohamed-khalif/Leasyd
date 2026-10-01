// Leasyd AI: ask about your systems in plain words; the AI SRE investigates logs, traces, metrics,
// checks and alerts and answers with the evidence. The start page shows live insights (computed from
// your data, no AI) to investigate; a conversation shows each step as it happens.
import { FormEvent, KeyboardEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { ai, AiConversation, AiStep, checks, promqlAt } from "../api";
import type { Ctx } from "../App";
import { fmtNum, rangeWindow } from "../time";

const SUGGESTIONS = [
  "What's broken right now?",
  "Summarize the errors in this time range",
  "Which service got slower, and why?",
  "Did anything change compared to yesterday?",
];

export function Ai({ ctx, path, params }: { ctx: Ctx; path: string; params: URLSearchParams }) {
  const id = path.split("/")[2] || null;
  const [list, setList] = useState<{ id: string; title: string; status: string; updated_at: string }[]>([]);
  const [enabled, setEnabled] = useState(true);
  const loadList = () => ai.list().then((r) => { setList(r.conversations); setEnabled(r.enabled); }, () => undefined);
  useEffect(() => { loadList(); }, [id]);   // eslint-disable-line react-hooks/exhaustive-deps

  // A question handed over from another page (?ask=...): ask it once, then show the conversation.
  const asked = useRef(false);
  useEffect(() => {
    const q = params.get("ask");
    if (q && !asked.current) {
      asked.current = true;
      let page: unknown;
      try { page = JSON.parse(params.get("page") ?? "null") ?? undefined; } catch { page = undefined; }
      ai.ask(q, { ...rangeWindow(ctx.range), page }).then((c) => ctx.go(`/ai/${c.id}`), (e) => alert((e as Error).message));
    }
  }, [params]);   // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="ai">
      <aside className="panel ai-history">
        <button className="btn primary" onClick={() => ctx.go("/ai")}>＋ New question</button>
        <div className="ai-history-head">Recent</div>
        {list.length === 0 && <div className="faint" style={{ padding: "0 12px" }}>Your questions show up here.</div>}
        {list.map((c) => (
          <button key={c.id} className={`ai-history-item${c.id === id ? " on" : ""}`} onClick={() => ctx.go(`/ai/${c.id}`)} title={c.title}>
            {c.status === "running" && <span className="ai-dot" />}{c.title}
          </button>
        ))}
      </aside>
      <div className="ai-main">
        {id ? <Conversation ctx={ctx} id={id} onChange={loadList} /> : <Start ctx={ctx} enabled={enabled} />}
      </div>
    </div>
  );
}

function Ask({ ctx, placeholder, onAsk, busy, autoFocus }: { ctx: Ctx; placeholder: string; onAsk: (q: string) => Promise<void>; busy?: boolean; autoFocus?: boolean }) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || sending || busy) return;
    setSending(true);
    try { await onAsk(text.trim()); setText(""); } finally { setSending(false); }
  };
  const key = (e: KeyboardEvent<HTMLTextAreaElement>) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  return (
    <form className="ai-ask" onSubmit={send}>
      <textarea className="input" rows={2} placeholder={placeholder} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={key}
                autoFocus={autoFocus} maxLength={4000} aria-label="Ask Leasyd AI" />
      <div className="ai-ask-foot">
        <span className="faint">Looks at {ctx.range.label.toLowerCase()} unless you say otherwise · reads your data, never changes it</span>
        <button className="btn primary" disabled={!text.trim() || sending || busy}>{sending ? "Asking…" : "Ask"}</button>
      </div>
    </form>
  );
}

function Start({ ctx, enabled }: { ctx: Ctx; enabled: boolean }) {
  const [error, setError] = useState<string | null>(null);
  const ask = async (q: string, page?: unknown) => {
    setError(null);
    try { const c = await ai.ask(q, { ...rangeWindow(ctx.range), page }); ctx.go(`/ai/${c.id}`); }
    catch (e) { setError((e as Error).message); }
  };
  return (
    <>
      <div className="ai-hero">
        <h1>Leasyd AI</h1>
        <p className="muted">Your AI SRE: ask about your services in plain words. It investigates your logs, traces, metrics, checks and alerts, and answers with the evidence.</p>
      </div>
      {!enabled && <div className="form-error">The AI SRE isn't set up for this deployment yet.</div>}
      <Ask ctx={ctx} placeholder="Ask anything, e.g. why is checkout slow?" onAsk={ask} autoFocus />
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="chips ai-suggest">{SUGGESTIONS.map((s) => <button key={s} type="button" className="chip" onClick={() => ask(s)}>{s}</button>)}</div>
      <Insights ctx={ctx} onAsk={ask} />
    </>
  );
}

// ------------------------------------------------------------------ live insights (no AI: computed from the data)

type Insight = { service: string; level: "bad" | "warn"; title: string; detail: string; question: string };

function Insights({ ctx, onAsk }: { ctx: Ctx; onAsk: (q: string, page?: unknown) => void }) {
  const [items, setItems] = useState<Insight[] | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setItems(null);
    const w = rangeWindow(ctx.range);
    const at = Math.floor(Date.parse(w.end) / 60_000) * 60;
    const r = Math.max(300, Math.round((Date.parse(w.end) - Date.parse(w.start)) / 60_000) * 60);
    const q = (text: string) => promqlAt({ promql: text, time: at }).then((x) => new Map(x.data.result.map((s) => [s.metric.service_name ?? s.metric.check_id ?? "", Number(s.value[1])])));
    const errPct = (off: string) => `100 * (sum by (service_name) (increase(leasyd.spans{status_code="ERROR"}[${r}s]${off})) or 0 * sum by (service_name) (increase(leasyd.spans[${r}s]${off}))) / sum by (service_name) (increase(leasyd.spans[${r}s]${off}))`;
    const p95 = (off: string) => `1000 * histogram_quantile(0.95, sum by (service_name, le) (rate(leasyd.span.duration{span_kind="SERVER"}[${r}s]${off})))`;
    const rate = (off: string) => `sum by (service_name) (rate(leasyd.spans{span_kind="SERVER"}[${r}s]${off}))`;
    const S = '{"synthetics.check.success", check_excluded=""}';
    const off = ` offset ${r}s`;
    Promise.all([q(errPct("")), q(errPct(off)), q(p95("")), q(p95(off)), q(rate("")), q(rate(off)),
                 q(`100 * sum by (check_id) (sum_over_time(${S}[${r}s])) / sum by (check_id) (count_over_time(${S}[${r}s]))`),
                 checks.list().then((c) => new Map(c.checks.map((x) => [x.id, x.name])), () => new Map<string, string>())])
      .then(([err, err0, lat, lat0, rps, rps0, up, names]) => {
        if (!live) return;
        const out: Insight[] = [];
        for (const [svc, e] of err) {
          const before = err0.get(svc) ?? 0;
          if (e >= 1 && e >= 2 * before) out.push({ service: svc, level: e >= 5 ? "bad" : "warn", title: `Error rate at ${fmtNum(e)}%`,
            detail: `${before ? `${fmtNum(before)}% in the period before` : "no errors in the period before"}`, question: `Why is ${svc}'s error rate at ${fmtNum(e)}%?` });
        }
        for (const [svc, v] of lat) {
          const before = lat0.get(svc);
          if (before && v >= 1.5 * before && v - before >= 20) out.push({ service: svc, level: v >= 3 * before ? "bad" : "warn",
            title: `p95 latency ${fmtNum(v)} ms`, detail: `up from ${fmtNum(before)} ms in the period before`, question: `Why did ${svc} get slower (p95 ${fmtNum(before)} → ${fmtNum(v)} ms)?` });
        }
        for (const [svc, v] of rps0) {
          const now = rps.get(svc) ?? 0;
          if (v >= 0.05 && now <= 0.5 * v) out.push({ service: svc, level: now === 0 ? "bad" : "warn", title: now === 0 ? "No traffic" : `Traffic down ${Math.round(100 - (100 * now) / v)}%`,
            detail: `${fmtNum(now)}/s, from ${fmtNum(v)}/s in the period before`, question: `Why did ${svc}'s traffic drop?` });
        }
        for (const [cid, v] of up) {
          const name = names.get(cid);
          if (name && v < 100) out.push({ service: `Check: ${name}`, level: v < 95 ? "bad" : "warn", title: `Uptime ${fmtNum(v)}%`,
            detail: "synthetic check runs failed in this range", question: `Why is the synthetic check "${name}" failing?` });
        }
        out.sort((a, b) => (a.level === b.level ? 0 : a.level === "bad" ? -1 : 1));
        setItems(out);
        setSel(out[0]?.service ?? null);
      }, () => live && setItems([]));
    return () => { live = false; };
  }, [ctx.range.key, ctx.range.from, ctx.range.to, ctx.tick]);   // eslint-disable-line react-hooks/exhaustive-deps

  const services = useMemo(() => {
    const m = new Map<string, Insight[]>();
    for (const i of items ?? []) m.set(i.service, [...(m.get(i.service) ?? []), i]);
    return [...m.entries()];
  }, [items]);
  const shown = services.find(([s]) => s === sel)?.[1] ?? [];
  return (
    <section className="ai-insights">
      <div className="ai-section">Live insights <span className="faint">· {ctx.range.label.toLowerCase()} compared with the period before</span></div>
      {items === null ? <div className="skeleton" style={{ height: 160 }} />
        : items.length === 0 ? <div className="panel" style={{ padding: 16 }}><span className="muted">Nothing unusual: no error, latency, traffic or check changes stand out.</span></div>
        : (
          <div className="panel ai-insight-grid">
            <div className="ai-insight-list">
              {services.map(([svc, is]) => (
                <button key={svc} className={`ai-insight-svc${svc === sel ? " on" : ""}`} onClick={() => setSel(svc)}>
                  <span className={`ai-badge ${is.some((i) => i.level === "bad") ? "bad" : "warn"}`}>{is.length}</span>{svc}
                </button>
              ))}
            </div>
            <div className="ai-insight-detail">
              <div className="faint">Explore {sel}</div>
              {shown.map((i, k) => (
                <div key={k} className={`ai-insight ${i.level}`}>
                  <div><b>{i.title}</b><div className="faint">{i.detail}</div></div>
                  <button className="btn" onClick={() => onAsk(i.question, { insight: i.title, service: i.service })}>Investigate</button>
                </div>
              ))}
            </div>
          </div>
        )}
    </section>
  );
}

// ------------------------------------------------------------------ a conversation

const TOOL_LABEL: Record<string, string> = {
  list_services: "Looked at every service", query_promql: "Ran a query", search_logs: "Searched logs", search_spans: "Searched spans",
  get_trace: "Opened a trace", top_values: "Grouped and counted", run_sql: "Ran SQL", synthetic_checks: "Checked synthetic checks", alerts: "Read alerts",
};

function Conversation({ ctx, id, onChange }: { ctx: Ctx; id: string; onChange: () => void }) {
  const [conv, setConv] = useState<AiConversation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rev, setRev] = useState(0);          // bumped by a follow-up question: poll again
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let live = true, timer: number | undefined;
    const poll = () => ai.get(id).then((c) => {
      if (!live) return;
      setConv(c); setError(null);
      if (c.status === "running") timer = window.setTimeout(poll, 1500); else onChange();
    }, (e) => live && setError((e as Error).message));
    if (!rev) setConv(null);
    poll();
    return () => { live = false; window.clearTimeout(timer); };
  }, [id, rev]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end", behavior: "smooth" }); }, [conv?.view.length]);
  if (error) return <div className="state error">{error}</div>;
  if (!conv) return <div className="skeleton" style={{ height: 300 }} />;

  // Group: each question, then its steps (collapsed once answered), then its answer.
  const turns: { q: AiStep; steps: AiStep[]; answer: AiStep[] }[] = [];
  for (const s of conv.view) {
    if (s.type === "question") turns.push({ q: s, steps: [], answer: [] });
    else if (turns.length) (s.type === "answer" || s.type === "error" ? turns[turns.length - 1].answer : turns[turns.length - 1].steps).push(s);
  }
  const running = conv.status === "running";
  const follow = async (q: string) => {
    try { setConv(await ai.ask(q, { conversation_id: conv.id, ...rangeWindow(ctx.range) })); setRev((r) => r + 1); }
    catch (e) { setError((e as Error).message); }
  };
  return (
    <div className="ai-conv">
      {turns.map((t, i) => {
        const last = i === turns.length - 1;
        return (
          <div key={i} className="ai-turn">
            <div className="ai-q">{t.q.type === "question" && t.q.text}</div>
            <Steps steps={t.steps} open={last && running} />
            {t.answer.map((a, j) => (
              <div key={j} className={`ai-answer${a.type === "error" ? " error" : ""}`}>{"text" in a && <Markdown text={a.text} ctx={ctx} />}</div>
            ))}
            {last && running && <div className="ai-working"><span className="ai-dot" />Investigating…</div>}
          </div>
        );
      })}
      <div ref={bottom} />
      <Ask ctx={ctx} placeholder="Ask a follow-up…" onAsk={follow} busy={running} />
    </div>
  );
}

function Steps({ steps, open }: { steps: AiStep[]; open: boolean }) {
  const [show, setShow] = useState(false);
  if (!steps.length) return null;
  const tools = steps.filter((s) => s.type === "tool").length;
  const visible = open || show;
  return (
    <div className="ai-steps">
      {!open && <button className="linkbtn" onClick={() => setShow(!show)}>{show ? "Hide" : "Show"} how it investigated ({tools} step{tools === 1 ? "" : "s"})</button>}
      {visible && steps.map((s, i) => (
        s.type === "tool" ? (
          <div key={i} className={`ai-step tool${s.error ? " err" : ""}`} title={JSON.stringify(s.input, null, 1)}>
            <span className="ai-step-icon">⌁</span>
            <span>{TOOL_LABEL[s.name] ?? s.name}</span>
            <span className="mono faint ai-step-input">{describe(s.input)}</span>
            <span className="faint">{s.error ? `error: ${s.error}` : s.summary}</span>
          </div>
        ) : <div key={i} className="ai-step progress">{"text" in s ? s.text : ""}</div>
      ))}
    </div>
  );
}

function describe(input: Record<string, unknown>): string {
  const parts = Object.entries(input).filter(([k]) => k !== "start" && k !== "end").map(([k, v]) =>
    typeof v === "string" ? (k === "promql" || k === "sql" ? v : `${k}=${v}`) : `${k}=${JSON.stringify(v)}`);
  const s = parts.join(" · ");
  return s.length > 140 ? s.slice(0, 140) + "…" : s;
}

/** The answer's Markdown: paragraphs, headings, bullet and numbered lists, **bold**, `code` (a trace id opens the trace). */
function Markdown({ text, ctx }: { text: string; ctx: Ctx }) {
  const inline = (s: string, key: string): ReactNode[] => s.split(/(\*\*[^*]+\*\*|`[^`]+`)/).map((part, j) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <b key={key + j}>{inline(part.slice(2, -2), `${key}${j}b`)}</b>;
    if (part.startsWith("`") && part.endsWith("`")) {
      const v = part.slice(1, -1);
      return /^[0-9a-f]{32}$/.test(v)
        ? <a key={key + j} className="mono" href={`#/traces/${v}`} onClick={(e) => { e.preventDefault(); ctx.go(`/traces/${v}`); }}>{v}</a>
        : <code key={key + j}>{v}</code>;
    }
    return part;
  });
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length;) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    const h = l.match(/^#{1,4}\s+(.*)/);
    if (h) { blocks.push(<h4 key={i}>{inline(h[1], `h${i}`)}</h4>); i++; continue; }
    if (/^\s*([-*]|\d+\.)\s+/.test(l)) {
      const ordered = /^\s*\d+\./.test(l), items: ReactNode[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(<li key={i}>{inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""), `l${i}`)}</li>); i++;
      }
      blocks.push(ordered ? <ol key={`o${i}`}>{items}</ol> : <ul key={`u${i}`}>{items}</ul>);
      continue;
    }
    if (l.startsWith("```")) {
      const code: string[] = []; i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      blocks.push(<pre key={`c${i}`} className="code">{code.join("\n")}</pre>);
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*([-*]|\d+\.)\s+|```)/.test(lines[i])) para.push(lines[i++]);
    blocks.push(<p key={`p${i}`}>{inline(para.join(" "), `p${i}`)}</p>);
  }
  return <div className="md">{blocks}</div>;
}
