// Sign out after IDLE_MS without activity (docs: Security summary, "Who can access it"). The last
// activity time is shared by the app's tabs through localStorage, so working in one tab keeps the
// others signed in; it is checked every 30 s and the moment a tab is shown again (a laptop opened
// after a break signs out at once, before any data is shown).
import { useEffect } from "react";

export const IDLE_MS = 15 * 60 * 1000;
const KEY = "leasyd.lastActive";
const WRITE_EVERY_MS = 15_000;   // activity is written at most this often (mouse moves are many)
const EVENTS = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "scroll"] as const;

export function lastActive(): number | null {
  try {
    const v = Number(localStorage.getItem(KEY));
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch { return null; }
}

export function markActive(now = Date.now()): void {
  try { localStorage.setItem(KEY, String(now)); } catch { /* storage blocked: the check below sees no time and waits */ }
}

/** Idle for longer than IDLE_MS since the last activity any tab recorded. No record: not idle. */
export function isIdle(now = Date.now(), last = lastActive()): boolean {
  return last !== null && now - last > IDLE_MS;
}

/** While signed in, calls onIdle once the user has been idle for IDLE_MS. */
export function useIdleSignOut(signedIn: boolean, onIdle: () => void): void {
  useEffect(() => {
    if (!signedIn) return;
    if (isIdle()) { onIdle(); return; }      // the app opened after a long break
    markActive();
    let written = Date.now();
    const active = () => {
      const now = Date.now();
      if (now - written >= WRITE_EVERY_MS) { written = now; markActive(now); }
    };
    const check = () => { if (isIdle()) onIdle(); };
    const shown = () => { if (document.visibilityState === "visible") check(); };
    EVENTS.forEach((e) => window.addEventListener(e, active, { passive: true }));
    document.addEventListener("visibilitychange", shown);
    const timer = setInterval(check, 30_000);
    return () => {
      EVENTS.forEach((e) => window.removeEventListener(e, active));
      document.removeEventListener("visibilitychange", shown);
      clearInterval(timer);
    };
  }, [signedIn, onIdle]);
}
