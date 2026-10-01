// Settings: the account's plan and today's data against its daily limit, how to connect an app
// (endpoint + API keys, a new key shown once), and the team (invite, remove). Owners change keys
// and people; members see them.
import { FormEvent, useEffect, useState } from "react";
import { account, Account } from "../api";
import type { Ctx } from "../App";
import { Panel } from "../components/Panel";
import { getConfig } from "../config";
import { fmtNum, fmtUnit } from "../time";

const SCOPES = { ingest: "Send data", read: "Read data (API)" } as const;

export function Settings({ ctx }: { ctx: Ctx }) {
  const [acc, setAcc] = useState<Account | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newKey, setNewKey] = useState<{ key_id: string; scope: string; api_key: string } | null>(null);
  const [scope, setScope] = useState<"ingest" | "read">("ingest");
  const [invite, setInvite] = useState({ email: "", role: "member" as "member" | "owner" });
  const [note, setNote] = useState<string | null>(null);

  const load = () => account.get().then(setAcc, (e) => setError((e as Error).message));
  useEffect(() => { load(); }, [ctx.tick]);   // eslint-disable-line react-hooks/exhaustive-deps
  const act = async (f: () => Promise<unknown>, done?: string) => {
    setBusy(true); setError(null); setNote(null);
    try { await f(); if (done) setNote(done); await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };

  if (!acc) return error ? <div className="state error">{error}</div> : <div className="skeleton" style={{ height: 300 }} />;
  const owner = acc.you.role === "owner";
  const cap = acc.daily_cap_bytes ?? null, used = acc.today?.bytes ?? 0;
  const share = cap ? Math.min(1, used / cap) : 0;
  const allowance = acc.searches?.units_per_day ?? null, searched = acc.searches?.units_today ?? 0;
  const searchShare = allowance ? Math.min(1, searched / allowance) : 0;
  const meterColor = (f: number) => (f >= 1 ? "var(--sev-error)" : f > 0.8 ? "var(--sev-warn)" : "var(--accent)");
  const endpoint = getConfig().ingestUrl || "https://ingest.leasyd.com";
  const keyText = newKey?.api_key ?? "<your API key>";

  return (
    <div className="settings">
      {error && <div className="form-error" role="alert">{error}</div>}
      {note && <div className="form-note" role="status">{note}</div>}

      <Panel title="Plan" right={<span className="faint">{acc.company} · <span className="mono">{acc.tenant}</span></span>}>
        <div className="plan">
          <div>
            <div className="plan-name">{acc.trial_ends_at ? "Free trial" : acc.plan === "free" ? "Free" : acc.plan === "standard" ? "Pay as you go" : acc.plan}</div>
            <div className="muted">{cap ? `Up to ${fmtUnit(cap, "bytes")} of logs, traces and metrics a day, kept 30 days.` : "No daily limit. Data kept 30 days."}</div>
            {acc.trial_ends_at && <div className="muted">{Date.parse(acc.trial_ends_at) > Date.now() ? "Ends" : "Ended"} {acc.trial_ends_at.slice(0, 10)}.</div>}
          </div>
          <div className="plan-usage">
            <div className="faint">Today (UTC)</div>
            <div className="plan-num">{fmtUnit(used, "bytes")}{cap ? <span className="faint"> of {fmtUnit(cap, "bytes")}</span> : null}</div>
            {cap ? <div className="meter"><i style={{ width: `${share * 100}%`, background: meterColor(share) }} /></div> : null}
            <div className="faint">{fmtNum(acc.today?.records ?? 0)} records{acc.today?.refused_bytes ? ` · ${fmtUnit(acc.today.refused_bytes, "bytes")} refused over the limit` : ""}</div>
          </div>
          {allowance ? (
            <div className="plan-usage">
              <div className="faint">Searches today (UTC)</div>
              <div className="plan-num">{Math.round(searchShare * 100)}%<span className="faint"> of the daily allowance</span></div>
              <div className="meter"><i style={{ width: `${searchShare * 100}%`, background: meterColor(searchShare) }} /></div>
              <div className="faint">{fmtNum(searched)} of {fmtNum(allowance)} search units · longer time ranges use more</div>
            </div>
          ) : null}
        </div>
        {cap && share >= 1 && <div className="form-error" style={{ marginTop: 12 }}>Today's limit is reached: new data is refused until 00:00 UTC.</div>}
        {allowance && searchShare >= 1 && <div className="form-error" style={{ marginTop: 12 }}>Today's search allowance is used up: searches resume at 00:00 UTC.</div>}
      </Panel>

      <Panel title="Connect your app">
        <div className="connect">
          <p className="muted">Send OpenTelemetry data over OTLP/HTTP with an API key that sends data. Any OpenTelemetry SDK, or the Collector:</p>
          <pre className="code">{`OTEL_EXPORTER_OTLP_ENDPOINT=${endpoint}
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=${keyText}
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_SERVICE_NAME=my-service`}</pre>
          <p className="faint">Data shows up under Logs, Traces and Metrics within a minute.</p>
        </div>
      </Panel>

      <Panel title="API keys" flush right={<span className="faint">{acc.keys.length} of {acc.limits.keys}</span>}>
        {newKey && (
          <div className="newkey">
            <b>New key: copy it now, it won't be shown again.</b>
            <span className="faint">It starts working within about 2 minutes (until then, data sent with it is refused with 403).</span>
            <div className="newkey-row">
              <code className="mono">{newKey.api_key}</code>
              <button className="btn" onClick={() => navigator.clipboard?.writeText(newKey.api_key).then(() => setNote("Copied."))}>Copy</button>
              <button className="btn ghost" onClick={() => setNewKey(null)}>Done</button>
            </div>
          </div>
        )}
        <table className="dtable">
          <thead><tr><th>Key</th><th>Can</th><th>Created</th><th>Status</th>{owner && <th />}</tr></thead>
          <tbody>
            {acc.keys.length === 0 && <tr><td colSpan={5} className="faint">No keys yet{owner ? ": create one to send data." : "."}</td></tr>}
            {acc.keys.map((k) => (
              <tr key={k.key_id}>
                <td className="mono">{k.key_id}</td>
                <td>{SCOPES[k.scope] ?? k.scope}</td>
                <td className="faint">{k.created_at?.slice(0, 10)}</td>
                <td>{k.status === "expiring" ? `expires ${k.expires_at?.slice(0, 16).replace("T", " ")}` : "active"}</td>
                {owner && <td className="num"><button className="linkbtn danger" disabled={busy}
                  onClick={() => confirm(`Revoke key ${k.key_id}? Apps using it stop sending within a minute.`) && act(() => account.revokeKey(k.key_id), "Key revoked.")}>Revoke</button></td>}
              </tr>
            ))}
          </tbody>
        </table>
        {owner && (
          <div className="panel-foot">
            <select className="select" value={scope} onChange={(e) => setScope(e.target.value as "ingest" | "read")} aria-label="Key can">
              <option value="ingest">Send data (for your apps)</option>
              <option value="read">Read data (for scripts using the query API)</option>
            </select>
            <button className="btn primary" disabled={busy} onClick={() => act(async () => setNewKey(await account.createKey(scope)))}>Create key</button>
          </div>
        )}
      </Panel>

      <Panel title="Team" flush right={<span className="faint">{acc.users.length} of {acc.limits.users}</span>}>
        <table className="dtable">
          <thead><tr><th>Email</th><th>Role</th><th>Added</th>{owner && <th />}</tr></thead>
          <tbody>
            {acc.users.map((u) => (
              <tr key={u.email}>
                <td>{u.email}{u.email === acc.you.email && <span className="faint"> (you)</span>}</td>
                <td>{u.role === "owner" ? "Owner" : "Member"}</td>
                <td className="faint">{u.created_at?.slice(0, 10)}{u.invited_by ? ` by ${u.invited_by}` : ""}</td>
                {owner && <td className="num">{u.email !== acc.you.email && <button className="linkbtn danger" disabled={busy}
                  onClick={() => confirm(`Remove ${u.email}? They are signed out everywhere.`) && act(() => account.remove(u.email), `${u.email} removed.`)}>Remove</button>}</td>}
              </tr>
            ))}
          </tbody>
        </table>
        {owner ? (
          <form className="panel-foot" onSubmit={(e: FormEvent) => { e.preventDefault(); act(async () => { await account.invite(invite.email.trim(), invite.role); setInvite({ ...invite, email: "" }); }, `Invitation sent to ${invite.email.trim()}.`); }}>
            <input className="input grow" type="email" required placeholder="colleague@company.com" value={invite.email}
                   onChange={(e) => setInvite({ ...invite, email: e.target.value })} aria-label="Email" />
            <select className="select" value={invite.role} onChange={(e) => setInvite({ ...invite, role: e.target.value as "member" | "owner" })} aria-label="Role">
              <option value="member">Member: uses everything</option>
              <option value="owner">Owner: also manages people and keys</option>
            </select>
            <button className="btn primary" disabled={busy}>Invite</button>
          </form>
        ) : <div className="panel-foot faint">Only owners can invite people and manage API keys.</div>}
      </Panel>
    </div>
  );
}
