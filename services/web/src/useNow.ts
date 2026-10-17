import { useMemo } from "react";
import { Range, rangeWindow } from "./time";

/** "Now" (epoch ms), read again only when one of `refresh` changes. For views whose "now" isn't the
 *  page's time window (a trace's age, today's usage). */
export function useNow(...refresh: unknown[]): number {
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `refresh` is why "now" is read again; nothing in it is read
  return useMemo(() => Date.now(), refresh);
}

/** The page's time window ({start, end} ISO) from its one shared "now" (ctx.now: set when the range
 *  changes, on Refresh and on opening a page), so every panel on the page asks for the same window.
 *  `nonce`: a page's own re-run (Query Builder's Run, a check's Run now) takes a new "now". */
export function useRangeWindow(ctx: { range: Range; now: number }, nonce = 0): { start: string; end: string } {
  const now = useMemo(() => (nonce ? Date.now() : ctx.now), [ctx.now, nonce]);
  return useMemo(() => rangeWindow(ctx.range, now), [ctx.range, now]);
}
