// The one account loader (C12): the trial banner, Settings, Get started and Drop rules. No cache:
// each use asks when it mounts, on Refresh (tick), on reload() and, if asked, every pollMs.
import { useCallback, useEffect, useRef, useState } from "react";
import { account as api, Account } from "./api";

export type AccountLoad = { account: Account | null; error: string | null; reload: () => Promise<void> };

/** account: null until first loaded; it stays when a later load fails (error says why). */
export function useAccount(tick = 0, pollMs = 0): AccountLoad {
  const [state, setState] = useState<{ account: Account | null; error: string | null }>({ account: null, error: null });
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);   // StrictMode mounts twice
  const reload = useCallback(() => api.get().then(
    (account) => { if (mounted.current) setState({ account, error: null }); },
    (e: Error) => { if (mounted.current) setState((s) => ({ ...s, error: e.message })); }), []);
  useEffect(() => {
    reload();
    if (!pollMs) return;
    const t = setInterval(reload, pollMs);
    return () => clearInterval(t);
  }, [tick, pollMs, reload]);
  return { ...state, reload };
}
