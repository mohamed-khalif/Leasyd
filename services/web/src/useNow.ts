import { useMemo } from "react";
import { Range, rangeWindow } from "./time";

/** "Now" (epoch ms), read again only when one of `refresh` changes: the range, the Refresh tick, a
 *  page's own nonce. Pages compute their time window from it, so a refresh moves the window. */
export function useNow(...refresh: unknown[]): number {
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `refresh` is why "now" is read again; nothing in it is read
  return useMemo(() => Date.now(), refresh);
}

/** The range's window ({start, end} ISO), recomputed when the range or one of `refresh` changes. */
export function useRangeWindow(range: Range, ...refresh: unknown[]): { start: string; end: string } {
  const now = useNow(range, ...refresh);
  return useMemo(() => rangeWindow(range, now), [range, now]);
}
