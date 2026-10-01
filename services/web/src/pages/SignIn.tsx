import { FormEvent, useState } from "react";
import { signup } from "../api";
import { forgotPassword, resetPassword, signIn } from "../auth";
import { IconLogo } from "../icons";

// signin: email + password. new-password: first sign-in with an invitation's temporary password.
// forgot: email -> Cognito emails a code. reset: code + new password, then signed in.
// signup: company + email -> the account is made and a temporary password emailed. sent: says so.
type Step = "signin" | "new-password" | "forgot" | "reset" | "signup" | "sent";

const HEADINGS: Record<Step, [string, string]> = {
  signin: ["Sign in to Leasyd", "Logs, traces and metrics for your services."],
  "new-password": ["Choose a new password", "Your invitation used a temporary password."],
  forgot: ["Reset your password", "We'll email you a code to set a new one."],
  reset: ["Enter your code", "If an account exists for that email, a code is on its way. It expires in 1 hour."],
  signup: ["Start your 7-day free trial", "Every feature, up to 1 GB of logs, traces and metrics a day."],
  sent: ["Check your inbox", "We're setting up your account. Within a couple of minutes you'll get an email with a temporary password; sign in with it here."],
};

export function SignIn(props: { onSignedIn: () => void }) {
  // The website's "Start free trial" links to app.leasyd.com/#/signup.
  const [step, setStep] = useState<Step>(() => (window.location.hash.startsWith("#/signup") ? "signup" : "signin"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [newPw, setNewPw] = useState("");
  const [complete, setComplete] = useState<((pw: string) => Promise<void>) | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [company, setCompany] = useState("");
  const [website, setWebsite] = useState("");   // hidden: only bots fill it in

  const go = (s: Step) => { setStep(s); setError(null); setNote(null); };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null); setNote(null);
    try {
      if (step === "signin") {
        const r = await signIn(email, password);
        if (r.kind === "new-password") { setComplete(() => r.complete); go("new-password"); }
        else props.onSignedIn();
      } else if (step === "new-password") {
        await complete!(newPw);
        props.onSignedIn();
      } else if (step === "signup") {
        await signup(email.trim(), company.trim(), website);
        go("sent");
      } else if (step === "forgot") {
        await forgotPassword(email);
        setCode(""); setNewPw("");
        go("reset");
      } else {
        await resetPassword(email, code, newPw);
        const r = await signIn(email, newPw);   // straight in with the new password
        if (r.kind === "ok") props.onSignedIn();
        else { setPassword(""); go("signin"); setNote("Password changed. Please sign in."); }
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const newPasswordField = (autoFocus: boolean) => (
    <label>New password
      <input className="input" type="password" autoComplete="new-password" required minLength={12}
             value={newPw} onChange={(e) => setNewPw(e.target.value)} autoFocus={autoFocus} />
      <span className="faint">At least 12 characters, with upper and lower case letters and a number.</span>
    </label>
  );
  const emailField = (
    <label>Email
      <input className="input" type="email" autoComplete="username" required value={email}
             onChange={(e) => setEmail(e.target.value)} autoFocus />
    </label>
  );
  const button = { signin: ["Sign in", "Signing in…"], "new-password": ["Set password and continue", "Saving…"],
                   forgot: ["Email me a code", "Sending…"], reset: ["Set new password", "Saving…"],
                   signup: ["Create account", "Creating…"], sent: ["Sign in", "Sign in"] }[step];

  return (
    <div className="signin">
      <div className="signin-card">
        <IconLogo />
        <h1>{HEADINGS[step][0]}</h1>
        <div className="muted">{step === "reset" ? `${HEADINGS.reset[1]} (${email.trim()})` : HEADINGS[step][1]}</div>
        <form onSubmit={submit}>
          {step === "signin" && (
            <>
              {emailField}
              <label>Password
                <input className="input" type="password" autoComplete="current-password" required value={password}
                       onChange={(e) => setPassword(e.target.value)} />
              </label>
            </>
          )}
          {step === "new-password" && newPasswordField(true)}
          {step === "signup" && (
            <>
              <label>Company or team
                <input className="input" required minLength={2} maxLength={80} autoComplete="organization" value={company}
                       onChange={(e) => setCompany(e.target.value)} autoFocus />
              </label>
              <label>Work email
                <input className="input" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
              </label>
              <input className="hp" tabIndex={-1} autoComplete="off" aria-hidden="true" value={website}
                     onChange={(e) => setWebsite(e.target.value)} name="website" />
            </>
          )}
          {step === "forgot" && emailField}
          {step === "reset" && (
            <>
              <label>Code from the email
                <input className="input mono" inputMode="numeric" autoComplete="one-time-code" required value={code}
                       onChange={(e) => setCode(e.target.value)} autoFocus />
              </label>
              {newPasswordField(false)}
            </>
          )}
          {note && <div className="form-note" role="status">{note}</div>}
          {error && <div className="form-error" role="alert">{error}</div>}
          {step === "sent"
            ? <button type="button" className="btn primary" onClick={() => { setPassword(""); go("signin"); }}>Sign in</button>
            : <button className="btn primary" disabled={busy}>{busy ? button[1] : button[0]}</button>}
          {step === "signin" && <button type="button" className="linkbtn" onClick={() => go("forgot")}>Forgot password?</button>}
          {step === "signin" && <span className="faint signin-alt">New to Leasyd? <button type="button" className="linkbtn" onClick={() => go("signup")}>Create an account</button></span>}
          {step === "signup" && <span className="faint signin-alt">Already have an account? <button type="button" className="linkbtn" onClick={() => go("signin")}>Sign in</button></span>}
          {step === "sent" && <span className="faint">No email after a few minutes? Check your spam folder; you can sign up again after an hour.</span>}
          {step === "reset" && (
            <button type="button" className="linkbtn" disabled={busy}
                    onClick={async () => {
                      setError(null);
                      try { await forgotPassword(email); setNote("A new code has been sent."); } catch (err) { setError((err as Error).message); }
                    }}>Send a new code</button>
          )}
          {(step === "forgot" || step === "reset") && (
            <button type="button" className="linkbtn" onClick={() => go("signin")}>Back to sign in</button>
          )}
          {step === "reset" && (
            // Cognito answers the same for every email (it never reveals who has a login), so explain the silent cases.
            <span className="faint">No email after a few minutes? Check your spam folder. If you have never signed in,
              use the temporary password from your invitation email instead.</span>
          )}
        </form>
      </div>
    </div>
  );
}
