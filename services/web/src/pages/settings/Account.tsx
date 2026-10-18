// Settings › Account: your profile and your password.
import { FormEvent, useState } from "react";
import { changePassword } from "../../auth";
import { Field, Section, SettingsPage, SettingsProps } from "./parts";

export function Profile({ acc }: SettingsProps) {
  return (
    <SettingsPage title="Profile">
      <Section title="Email" about="You sign in with this address, and alerts and invitations go to it.">
        <Field label="Email"><input className="input" value={acc.you.email} readOnly aria-label="Email" /></Field>
        <p className="faint">To use another address, ask an owner to invite it, then remove this one.</p>
      </Section>
      <Section title="Role" about={<>Owners manage people, API keys, billing and GPU prices. Members use everything else.</>}>
        <div className="set-value">{acc.you.role === "owner" ? "Owner" : "Member"}</div>
        <p className="faint">in {acc.company} (<span className="mono">{acc.tenant}</span>)</p>
      </Section>
    </SettingsPage>
  );
}

export function Security() {
  const [form, setForm] = useState({ current: "", next: "", again: "" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (form.next !== form.again) { setMsg({ ok: false, text: "The new passwords don't match." }); return; }
    setBusy(true); setMsg(null);
    try {
      await changePassword(form.current, form.next);
      setForm({ current: "", next: "", again: "" });
      setMsg({ ok: true, text: "Password changed. Use the new one next time you sign in." });
    } catch (err) { setMsg({ ok: false, text: (err as Error).message }); } finally { setBusy(false); }
  };
  return (
    <SettingsPage title="Security">
      <Section title="Password" about={<>At least 12 characters, with upper and lower case letters and a number.
        Forgot your current one? Sign out and use “Forgot password?” on the sign-in page.</>}>
        <form className="set-form" onSubmit={submit}>
          <Field label="Current password">
            <input className="input" type="password" autoComplete="current-password" required value={form.current}
                   onChange={(e) => setForm({ ...form, current: e.target.value })} />
          </Field>
          <Field label="New password">
            <input className="input" type="password" autoComplete="new-password" required minLength={12} value={form.next}
                   onChange={(e) => setForm({ ...form, next: e.target.value })} />
          </Field>
          <Field label="New password again">
            <input className="input" type="password" autoComplete="new-password" required value={form.again}
                   onChange={(e) => setForm({ ...form, again: e.target.value })} />
          </Field>
          {msg && <div className={msg.ok ? "form-note" : "form-error"} role={msg.ok ? "status" : "alert"}>{msg.text}</div>}
          <div><button className="btn primary" disabled={busy}>Change password</button></div>
        </form>
      </Section>
    </SettingsPage>
  );
}
