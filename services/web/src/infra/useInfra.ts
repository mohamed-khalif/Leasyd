// React side of the Kubernetes and GPU pages: run queries.ts's queries with the services hint, and
// load the account's GPU prices. Rows come back typed as the queries declare them.
import { useEffect, useState } from "react";
import { account, records, sql } from "../api";
import type { GpuPrice } from "./model";
import { discovery, Q, Uses } from "./queries";

export type Hint = Partial<Record<Uses, string[]>>;
export type Rows<T> = { rows: T[]; error: string | null; loading: boolean };

const DISCOVERY_MINUTES = 15;

/** Which services carry GPU, Kubernetes and event data, from the last 15 minutes of the window
 *  (cheap). undefined while loading; {} when unknown, so queries read everything. */
export function useHint(w: { start: string; end: string }, key: string): Hint | undefined {
  const [hint, setHint] = useState<Hint | undefined>(undefined);
  useEffect(() => {
    let live = true;
    const end = Date.parse(w.end), start = Math.max(Date.parse(w.start), end - DISCOVERY_MINUTES * 60_000);
    sql({ sql: discovery().sql, start: new Date(start).toISOString(), end: w.end }).then((r) => {
      if (!live) return;
      const out: Hint = {};
      for (const row of records(r)) (out[row.kind as Uses] ??= []).push(String(row.service));
      setHint(out);
    }, () => live && setHint({}));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed: `key` encodes the window and refresh
  }, [key]);
  return hint;
}

/** The services a query should read, or undefined (all) when any data it uses wasn't found. */
export function servicesFor(q: Q, hint: Hint): string[] | undefined {
  if (!q.uses.length || q.uses.some((u) => !hint[u]?.length)) return undefined;
  return [...new Set(q.uses.flatMap((u) => hint[u] ?? []))].sort();
}

/** Runs a query (null: none) over the window once the hint is known; reruns when `key` changes. */
export function useRows<T>(q: Q | null, w: { start: string; end: string }, key: string, hint: Hint | undefined): Rows<T> {
  const [state, setState] = useState<Rows<T>>({ rows: [], error: null, loading: true });
  const ready = hint !== undefined;
  useEffect(() => {
    if (!q || !ready) return;
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    sql({ sql: q.sql, ...w, services: servicesFor(q, hint!) }).then(
      (r) => live && setState({ rows: records(r) as unknown as T[], error: null, loading: false }),
      (e: Error) => live && setState({ rows: [], error: e.message, loading: false }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed: `key` encodes window and refresh; the hint is waited for via `ready`
  }, [key, ready, q?.sql]);
  return state;
}

/** The account's own GPU prices ([] when none are set), and why they couldn't be read (costs then
 *  use the list prices, which the page must say). */
export function useGpuPrices(tick: number): { prices: GpuPrice[]; error: string | null } {
  const [state, setState] = useState<{ prices: GpuPrice[]; error: string | null }>({ prices: [], error: null });
  useEffect(() => {
    account.gpuPrices().then((r) => setState({ prices: r.prices ?? [], error: null }), (e: Error) => setState({ prices: [], error: e.message }));
  }, [tick]);
  return state;
}

/** Adapts Rows to what <Loads> expects. */
export const loads = <T,>(r: Rows<T>) => ({ data: r.loading ? null : ({} as never), error: r.error, loading: r.loading });
