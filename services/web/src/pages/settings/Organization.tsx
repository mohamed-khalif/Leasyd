// Settings › Organization: the account itself and its people.
import { FormEvent, useState } from "react";
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
