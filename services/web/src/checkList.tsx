// The account's synthetic checks for pages that name or pick them, with the error when the list
// can't be read (shown, never taken as "no checks").
import { useEffect, useState } from "react";
import { Check, checks } from "./api";

export type CheckList = { checks: Check[] | null; error: string | null };

/** null checks while loading or when they can't be read (then error says why). */
export function useCheckList(tick = 0): CheckList {
  const [state, setState] = useState<CheckList>({ checks: null, error: null });
  useEffect(() => {
    let live = true;
    checks.list().then((r) => live && setState({ checks: r.checks, error: null }), (e: Error) => live && setState({ checks: null, error: e.message }));
    return () => { live = false; };
  }, [tick]);
  return state;
}

export function CheckListError({ error }: { error: string | null }) {
  return error ? <div className="form-error" role="alert">Couldn't read your synthetic checks: {error}</div> : null;
}
