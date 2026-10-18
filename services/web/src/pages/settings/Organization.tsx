// Settings › Organization: the account itself, its people, and today's usage against its limits.
import { FormEvent, useState } from "react";
import { fmtNum, fmtUnit } from "../../lib/format";
import { account } from "../../api";
import { Field, ownersOnly, Section, SettingsPage, SettingsProps } from "./parts";

export function Organization({ acc, setNote }: SettingsProps) {
  return (
    <SettingsPage title="Organization">
      <Section title="Name" about="Your company, as shown to your team and on invitations.">
        <div className="set-value">{acc.company}</div>
        <p className="faint">To rename it, email support@leasyd.com.</p>
      </Section>
      <Section title="Account ID" about="Quote this when you contact support.">
        <div className="newkey-row">
          <code className="mono">{acc.tenant}</code>
          <button className="btn" onClick={() => navigator.clipboard?.writeText(acc.tenant).then(() => setNote("Copied."))}>Copy</button>
        </div>
      </Section>
      <Section title="Data kept" about="How long logs, traces and metrics stay searchable.">
        <div className="set-value">30 days</div>
        <p className="faint">Discard data you don't need as it arrives with <a href="#/drop-rules">drop rules</a>.</p>
      </Section>
    </SettingsPage>
  );
}

export function Team({ acc, owner, busy, act }: SettingsProps) {
  const [invite, setInvite] = useState({ email: "", role: "member" as "member" | "owner" });
  const send = (e: FormEvent) => {
    e.preventDefault();
    const email = invite.email.trim();
    act(async () => { await account.invite(email, invite.role); setInvite({ ...invite, email: "" }); }, `Invitation sent to ${email}.`);
  };
  return (
    <SettingsPage title="Team">
      <Section title="Invite someone" about="They get an email with a temporary password and choose their own when they first sign in.">
        {owner ? (
          <form className="set-form" onSubmit={send}>
            <Field label="Email">
              <input className="input" type="email" required placeholder="colleague@company.com" value={invite.email}
                     onChange={(e) => setInvite({ ...invite, email: e.target.value })} aria-label="Email" />
            </Field>
            <Field label="Role">
              <select className="select" value={invite.role} onChange={(e) => setInvite({ ...invite, role: e.target.value as "member" | "owner" })} aria-label="Role">
                <option value="member">Member: uses everything</option>
                <option value="owner">Owner: also manages people and keys</option>
              </select>
            </Field>
            <div><button className="btn primary" disabled={busy}>Invite</button></div>
          </form>
        ) : ownersOnly("invite people")}
      </Section>
      <Section title="People" about={`${acc.users.length} of ${acc.limits.users}. Someone removed is signed out everywhere.`}>
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
      </Section>
    </SettingsPage>
  );
}

const meterColor = (f: number) => (f >= 1 ? "var(--sev-error)" : f > 0.8 ? "var(--sev-warn)" : "var(--accent)");

export function Usage({ acc }: SettingsProps) {
  const cap = acc.daily_cap_bytes ?? null, used = acc.today?.bytes ?? 0;
  const share = cap ? Math.min(1, used / cap) : 0;
  const allowance = acc.searches?.units_per_day ?? null, searched = acc.searches?.units_today ?? 0;
  const searchShare = allowance ? Math.min(1, searched / allowance) : 0;
  return (
    <SettingsPage title="Usage" lead={<>Today so far. For the month and the cost, see <a href="#/usage">Usage &amp; Cost</a>.</>}>
      <Section title="Data received today" about={cap ? `Your plan takes up to ${fmtUnit(cap, "bytes")} of logs, traces and metrics a day (UTC). Over that, new data is refused until 00:00 UTC.` : "No daily limit."}>
        <div className="plan-usage">
          <div className="plan-num">{fmtUnit(used, "bytes")}{cap ? <span className="faint"> of {fmtUnit(cap, "bytes")}</span> : null}</div>
          {cap ? <div className="meter"><i style={{ width: `${share * 100}%`, background: meterColor(share) }} /></div> : null}
          <div className="faint">{fmtNum(acc.today?.records ?? 0)} records{acc.today?.refused_bytes ? ` · ${fmtUnit(acc.today.refused_bytes, "bytes")} refused over the limit` : ""}</div>
        </div>
        {cap && share >= 1 && <div className="form-error">Today's limit is reached: new data is refused until 00:00 UTC.</div>}
      </Section>
      {allowance ? (
        <Section title="Searches today" about="Each search uses units; longer time ranges use more. The allowance resets at 00:00 UTC.">
          <div className="plan-usage">
            <div className="plan-num">{Math.round(searchShare * 100)}%<span className="faint"> of the daily allowance</span></div>
            <div className="meter"><i style={{ width: `${searchShare * 100}%`, background: meterColor(searchShare) }} /></div>
            <div className="faint">{fmtNum(searched)} of {fmtNum(allowance)} search units</div>
          </div>
          {searchShare >= 1 && <div className="form-error">Today's search allowance is used up: searches resume at 00:00 UTC.</div>}
        </Section>
      ) : null}
    </SettingsPage>
  );
}
