import { FormEvent, useState } from "react";
import { startDemo } from "../auth";
import { IconLogo } from "../icons";

// The live demo's door (#/demo, linked from the website and emails): a work email, then the real
// app on a sample business's live data, read-only, starting on the AI agents page.
export function DemoGate(props: { onStarted: () => void }) {
  const [email, setEmail] = useState(() => (new URLSearchParams(window.location.hash.split("?")[1] || "").get("email") || "").slice(0, 254));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await startDemo(email);
      window.location.hash = "/agents";
      props.onStarted();
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
        <h1>Explore the live demo</h1>
        <div className="muted">A sample business's live data: its AI support agent, services, traces, logs, Kubernetes and GPUs. Read-only, no setup.</div>
        <form onSubmit={submit}>
          <label>Work email
            <input className="input" type="email" autoComplete="email" required value={email}
                   onChange={(e) => setEmail(e.target.value)} autoFocus />
          </label>
          {error && <div className="form-error" role="alert">{error}</div>}
          <button className="btn primary" disabled={busy}>{busy ? "Opening…" : "Open the demo"}</button>
          <span className="faint signin-alt">Already have an account? <a href="#/">Sign in</a></span>
        </form>
      </div>
    </div>
  );
}
