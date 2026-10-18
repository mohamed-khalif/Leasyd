// Building blocks of the Settings pages: a page (title, then sections), and a section with its
// title and explanation on the left and its controls on the right.
import { ReactNode } from "react";
import type { Account } from "../../api";
import type { Ctx } from "../../App";

/** What every Settings page gets: the account, whether you're an owner, and `act`, which runs a
 *  change with the page's busy / error / done note and reloads the account after it. */
export type SettingsProps = {
  ctx: Ctx; acc: Account; owner: boolean; busy: boolean;
  act: (f: () => Promise<unknown>, done?: string) => Promise<void>;
  setNote: (text: string) => void;
  reload: () => void;
};

export function SettingsPage({ title, lead, children }: { title: string; lead?: ReactNode; children: ReactNode }) {
  return (
    <div className="set-page">
      <h1>{title}</h1>
      {lead && <p className="muted set-lead">{lead}</p>}
      {children}
    </div>
  );
}

export function Section({ title, about, children }: { title: string; about?: ReactNode; children: ReactNode }) {
  return (
    <section className="set-section">
      <div className="set-about"><h2>{title}</h2>{about && <div className="muted">{about}</div>}</div>
      <div className="set-body">{children}</div>
    </section>
  );
}

/** A label above its control, for forms in a section. */
export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="set-field">
      <span className="set-label">{label}</span>
      {children}
      {hint && <span className="faint">{hint}</span>}
    </label>
  );
}

export const ownersOnly = (what: string) => <p className="faint">Only the account's owners can {what}.</p>;
