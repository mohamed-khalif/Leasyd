// The account's synthetic checks for pages that name or pick them, with the error when the list
// can't be read (shown, never taken as "no checks").
import { useEffect, useState } from "react";
import { Check, checks } from "./api";

export type CheckList = { checks: Check[] | null; limit: number | null; error: string | null };

/** The one check-list loader (asks on mount and when `tick` changes; nothing is kept between pages).
 *  null checks while loading or when they can't be read (then error says why); limit: how many the
 *  account may have. */
export function useCheckList(tick = 0, enabled = true): CheckList {
  const [state, setState] = useState<CheckList>({ checks: null, limit: null, error: null });
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    checks.list().then((r) => live && setState({ checks: r.checks, limit: r.limit, error: null }),
                       (e: Error) => live && setState({ checks: null, limit: null, error: e.message }));
    return () => { live = false; };
  }, [tick, enabled]);
  return state;
}

/** Check names by id, for naming series grouped by check_id. */
export const checkNameMap = (list: Check[]) => new Map(list.map((c) => [c.id, c.name] as [string, string]));

export function CheckListError({ error }: { error: string | null }) {
  return error ? <div className="form-error" role="alert">Couldn't read your synthetic checks: {error}</div> : null;
}
