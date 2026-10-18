// Settings: a menu on the left (your account; the organization) and one page per topic on the right,
// at #/settings/<page>. Owners change people, keys, billing and prices; members see them.
import { ReactNode, useCallback, useState } from "react";
import type { Ctx } from "../App";
import { useAccount } from "../useAccount";
import { Profile, Security } from "./settings/Account";
import { Organization, Team } from "./settings/Organization";
import { Usage } from "./settings/UsageCost";
import { Billing } from "./settings/Billing";
import { ApiKeys, AiTools } from "./settings/ApiKeys";
import { GpuPrices } from "./settings/GpuPrices";
import type { SettingsProps } from "./settings/parts";
import { SETTINGS_MENU, SettingsId, settingsPage } from "./settings/menu";

const PAGES: Record<SettingsId, (p: SettingsProps) => ReactNode> = {
  profile: (p) => <Profile {...p} />,
  security: () => <Security />,
  organization: (p) => <Organization {...p} />,
  team: (p) => <Team {...p} />,
  usage: (p) => <Usage {...p} />,
  billing: (p) => <Billing {...p} />,
  "api-keys": (p) => <ApiKeys {...p} />,
  "gpu-prices": (p) => <GpuPrices {...p} />,
  "ai-tools": () => <AiTools />,
};

export function Settings({ ctx, path }: { ctx: Ctx; path: string }) {
  const { account: acc, error: loadError, reload } = useAccount(ctx.tick);
  const page = settingsPage(path);
  // A done note or an error belongs to the page it happened on; another page doesn't show it.
  const [said, setSaid] = useState<{ on: string; error?: string; note?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const setNote = useCallback((note: string) => setSaid({ on: page.id, note }), [page.id]);
  const error = said?.on === page.id ? said.error : undefined, note = said?.on === page.id ? said.note : undefined;

  const act = useCallback(async (f: () => Promise<unknown>, done?: string) => {
    setBusy(true); setSaid(null);
    try { await f(); if (done) setSaid({ on: page.id, note: done }); await reload(); }
    catch (e) { setSaid({ on: page.id, error: (e as Error).message }); } finally { setBusy(false); }
  }, [reload, page.id]);

  return (
    <div className="set-layout">
      <nav className="set-menu" aria-label="Settings">
        {SETTINGS_MENU.map((g) => (
          <div key={g.group}>
            <div className="set-group">{g.group}</div>
            {g.pages.map((p) => (
              <a key={p.id} href={`#/settings/${p.id}`} className={p.id === page.id ? "active" : undefined}
                 aria-current={p.id === page.id ? "page" : undefined}>{p.label}</a>
            ))}
          </div>
        ))}
      </nav>
      <div className="set-main settings">
        {(error ?? loadError) && <div className="form-error" role="alert">{error ?? loadError}</div>}
        {note && <div className="form-note" role="status">{note}</div>}
        {acc ? PAGES[page.id]({ ctx, acc, owner: acc.you.role === "owner", busy, act, setNote, reload })
          : !(error ?? loadError) && <div className="skeleton" style={{ height: 300 }} />}
      </div>
    </div>
  );
}
