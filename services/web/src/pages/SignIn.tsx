import { FormEvent, useState } from "react";
import { signIn } from "../auth";
import { IconLogo } from "../icons";

export function SignIn(props: { onSignedIn: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [next, setNext] = useState<((pw: string) => Promise<void>) | null>(null);
  const [newPw, setNewPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      if (next) {
        await next(newPw);
        props.onSignedIn();
      } else {
        const r = await signIn(email, password);
        if (r.kind === "new-password") setNext(() => r.complete);
        else props.onSignedIn();
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="signin">
      <div className="signin-card">
        <IconLogo />
        <h1>{next ? "Choose a new password" : "Sign in to Leasyd"}</h1>
        <div className="muted">{next ? "Your invitation used a temporary password." : "Logs, traces and metrics for your services."}</div>
        <form onSubmit={submit}>
          {next ? (
            <label>New password
              <input className="input" type="password" autoComplete="new-password" required minLength={12}
                     value={newPw} onChange={(e) => setNewPw(e.target.value)} autoFocus />
              <span className="faint">At least 12 characters, with upper and lower case letters and a number.</span>
            </label>
          ) : (
            <>
              <label>Email
                <input className="input" type="email" autoComplete="username" required value={email}
                       onChange={(e) => setEmail(e.target.value)} autoFocus />
              </label>
              <label>Password
                <input className="input" type="password" autoComplete="current-password" required value={password}
                       onChange={(e) => setPassword(e.target.value)} />
              </label>
            </>
          )}
          {error && <div className="form-error" role="alert">{error}</div>}
          <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : next ? "Set password and continue" : "Sign in"}</button>
        </form>
      </div>
    </div>
  );
}
