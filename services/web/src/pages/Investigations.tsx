import { FormEvent, MouseEvent, useEffect, useState } from "react";
import { ai, Investigation, InvestigationRow, investigations } from "../api";
import type { Ctx } from "../App";
import { fmtAgo, fmtTs } from "../lib/format";
import { Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";

// Automatic investigations: every firing alert (or webhook) Leasyd investigated on its own: the
// finding, its evidence, where it was sent, the team's Right/Wrong, and a way to ask a follow-up.

const STATUS: Record<InvestigationRow["status"], string> = { queued: "queued", running: "investigating…", completed: "done", failed: "failed", skipped: "skipped (hourly limit)" };

/** An evidence link points at app.leasyd.com/#/...: inside the app, follow its route instead. */
const route = (link: string) => (link.includes("#/") ? link.slice(link.indexOf("#") + 1) : null);

function confidence(c: number | null | undefined) {
  if (c == null) return "";
  const word = c >= 0.8 ? "high" : c >= 0.5 ? "medium" : c > 0.3 ? "low" : "not sure";
  return `${c.toFixed(2)} (${word})`;
}

export function Investigations({ ctx, path }: { ctx: Ctx; path: string }) {
  const id = path.split("/")[2];
  return id ? <Detail ctx={ctx} id={id} /> : <List ctx={ctx} />;
}

function List({ ctx }: { ctx: Ctx }) {
  const [rows, setRows] = useState<InvestigationRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { investigations.list().then((d) => setRows(d.investigations), (e: Error) => setError(e.message)); }, [ctx.tick]);
  if (error) return <div className="form-error" role="alert">{error}</div>;
  return (
    <Panel title="Automatic investigations" flush>
      {!rows ? <div className="skeleton" style={{ height: 160 }} /> : !rows.length ? (
        <div className="state" style={{ minHeight: 120 }}>
          None yet. When an alert rule fires, Leasyd investigates it and sends the root cause to your channels.
          <button className="btn" onClick={() => ctx.go("/alerts/channels")}>Choose where they go</button>
        </div>
      ) : (
        <RankTable head={["alert", "root cause", "confidence", "team said", "status", "when"]} maxHeight={640}
                   rows={rows.map((r) => [
                     <a key={r.id} className="link" href={`#/investigations/${r.id}`} onClick={(e) => { e.preventDefault(); ctx.go(`/investigations/${r.id}`); }}>{r.title}</a>,
                     r.root_cause ?? "", confidence(r.confidence), r.verdict ?? "", STATUS[r.status] ?? r.status, fmtAgo(r.created_at)])} />
      )}
    </Panel>
  );
}

function Detail({ ctx, id }: { ctx: Ctx; id: string }) {
  const [inv, setInv] = useState<Investigation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let live = true, timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => investigations.get(id).then((d) => {
      if (!live) return;
      setInv(d);
      if (d.status === "queued" || d.status === "running") timer = setTimeout(load, 4000);    // until it finishes
    }, (e: Error) => live && setError(e.message));
    load();
    return () => { live = false; clearTimeout(timer); };
  }, [id, ctx.tick, nonce]);
  if (error) return <div className="form-error" role="alert">{error}</div>;
  if (!inv) return <div className="skeleton" style={{ height: 300 }} />;
  const r = inv.result, t = inv.trigger;
  const go = (link: string) => (e: MouseEvent) => { const to = route(link); if (to) { e.preventDefault(); ctx.go(to); } };
  return (
    <>
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <a className="btn" href="#/investigations" onClick={(e) => { e.preventDefault(); ctx.go("/investigations"); }}>← All investigations</a>
        {t.url && <a className="btn" href={t.url} target="_blank" rel="noreferrer">The alert</a>}
      </div>
      <Panel title={t.title}>
        <p className="faint" style={{ marginTop: 0 }}>
          {t.source} · {t.severity} · started {fmtTs(t.started_at)} · {STATUS[inv.status] ?? inv.status}
          {inv.versions?.model ? ` · ${inv.versions.model}` : ""}{inv.cost?.usd != null ? ` · $${inv.cost.usd.toFixed(2)}` : ""}
        </p>
        {r ? (<>
          <p><b>Root cause:</b> {r.root_cause}<br /><span className="faint">confidence {confidence(r.confidence)}</span></p>
          <p>{r.summary}</p>
          {r.next_step && <p><b>Next step:</b> {r.next_step}</p>}
        </>) : inv.status === "skipped" ? <p>Not investigated: the account's hourly limit of automatic investigations was reached.</p>
          : inv.status === "failed" ? <p>The investigation couldn't finish. Its log is below.</p>
          : <p className="faint">Investigating… this page updates by itself.</p>}
        {r && <Verdict inv={inv} onSaved={() => setNonce((n) => n + 1)} />}
      </Panel>
      {r && r.evidence.length > 0 && (
        <Panel title="Evidence" flush>
          <RankTable head={["", "what it shows", "source"]} maxHeight={480}
                     rows={r.evidence.map((e) => [e.id, e.claim || e.summary,
                       <a key={e.id} className="link" href={e.link} onClick={go(e.link)}>{e.title}</a>])} />
        </Panel>
      )}
      {r && r.ruled_out.length > 0 && (
        <Panel title="Ruled out"><ul style={{ margin: 0, paddingLeft: 18 }}>{r.ruled_out.map((x, i) => <li key={i}>{x}</li>)}</ul></Panel>
      )}
      {r && <FollowUp ctx={ctx} id={inv.id} />}
      <Panel title="What happened" flush>
        <RankTable head={["when", "what", "detail"]} maxHeight={360}
                   rows={inv.events.map((e) => [fmtTs(e.at), e.type.replace(/_/g, " "), e.detail])} />
      </Panel>
      {inv.tool_calls && inv.tool_calls.length > 0 && (
        <Panel title="Queries the agent ran (read-only)" flush>
          <RankTable head={["tool", "input", "ms", "result"]} maxHeight={360}
                     rows={inv.tool_calls.map((c) => [c.name, <span key="i" className="mono">{JSON.stringify(c.input)}</span>, c.ms, c.evidence ?? `error: ${c.error}`])} />
        </Panel>
      )}
    </>
  );
}

function Verdict({ inv, onSaved }: { inv: Investigation; onSaved: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async (verdict: "right" | "wrong") => {
    setBusy(true); setError(null);
    try { await investigations.feedback(inv.id, verdict, note); onSaved(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const f = inv.feedback;
  return (
    <div className="result-box" style={{ marginTop: 12 }}>
      {f ? <div><b>Marked {f.verdict}</b> by {f.by === "link" ? "a link in the message" : f.by} {fmtAgo(f.at)}{f.note ? `: “${f.note}”` : ""}. You can change it.</div>
         : <div><b>Was this right?</b> Your answer is kept and used by later investigations.</div>}
      <div className="toolbar" style={{ marginTop: 8 }}>
        <input className="input" placeholder="What was it, if not this? (optional)" value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} />
        <button className="btn primary" disabled={busy} onClick={() => save("right")}>Right</button>
        <button className="btn" disabled={busy} onClick={() => save("wrong")}>Wrong</button>
      </div>
      {error && <div className="form-error" role="alert">{error}</div>}
    </div>
  );
}

function FollowUp({ ctx, id }: { ctx: Ctx; id: string }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ask = async (e: FormEvent) => {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true); setError(null);
    try { const c = await ai.ask(q.trim(), { investigation: id }); ctx.go(`/ai/${c.id}`); }
    catch (err) { setError((err as Error).message); setBusy(false); }
  };
  return (
    <Panel title="Ask a follow-up">
      <form onSubmit={ask} className="toolbar">
        <input className="input" style={{ flex: 1 }} placeholder="e.g. Did this happen before the deploy? What else calls fraud-api?" value={q}
               maxLength={4000} onChange={(e) => setQ(e.target.value)} />
        <button className="btn primary" disabled={busy || !q.trim()}>{busy ? "Asking…" : "Ask Leasyd AI"}</button>
      </form>
      <p className="faint" style={{ marginBottom: 0 }}>The AI SRE starts from this investigation's finding and evidence, over the incident's time window.</p>
      {error && <div className="form-error" role="alert">{error}</div>}
    </Panel>
  );
}
