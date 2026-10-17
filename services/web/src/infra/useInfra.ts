// React side of the Kubernetes and GPU pages: run queries.ts's queries with the services hint, and
// load the account's GPU prices. Rows come back typed as the queries declare them.
import { useEffect, useMemo, useState } from "react";
import { account, records, sql } from "../api";
import { useLoad } from "../useQuery";
import type { GpuPrice } from "./model";
import { discovery, Q, Uses } from "./queries";

export type Hint = Partial<Record<Uses, string[]>>;
export type Rows<T> = { rows: T[]; error: string | null; loading: boolean; truncated?: true };   // truncated: cut at the row cap

const DISCOVERY_MINUTES = 15;

/** Which services carry GPU, Kubernetes and event data, from the last 15 minutes of the window
 *  (cheap). undefined while loading; {} when unknown, so queries read everything. On a new key the
 *  previous hint stays until the new one comes (code-audit C6). */
export function useHint(w: { start: string; end: string }, key: string): Hint | undefined {
  const end = Date.parse(w.end), start = Math.max(Date.parse(w.start), end - DISCOVERY_MINUTES * 60_000);
  const got = useLoad((signal) => sql({ sql: discovery().sql, start: new Date(start).toISOString(), end: w.end }, { signal }).then((r) => {
    const out: Hint = {};
    for (const row of records(r)) (out[row.kind as Uses] ??= []).push(String(row.service));
    return out;
  }, () => ({}) as Hint), key);
  return got.data ?? undefined;
}

/** The services a query should read, or undefined (all) when any data it uses wasn't found. */
export function servicesFor(q: Q, hint: Hint): string[] | undefined {
  if (!q.uses.length || q.uses.some((u) => !hint[u]?.length)) return undefined;
  return [...new Set(q.uses.flatMap((u) => hint[u] ?? []))].sort();
}

/** Runs a query (null: none) over the window once the hint is known; reruns when `key` changes. */
export function useRows<T>(q: Q | null, w: { start: string; end: string }, key: string, hint: Hint | undefined): Rows<T> {
  const ready = hint !== undefined;
  const got = useLoad(q && ready ? (signal) => sql({ sql: q.sql, ...w, services: servicesFor(q, hint!) }, { signal }) : null,
                      `${key}|${ready}|${q?.sql}`, true);
  const rows = useMemo(() => (got.data ? records(got.data) : []) as unknown as T[], [got.data]);   // same array until new data
  return { rows, error: got.error, loading: got.loading, ...(got.data?.truncated ? { truncated: true as const } : {}) };
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
export const loads = <T,>(r: Rows<T>) => ({ data: r.loading ? null : ({ truncated: r.truncated } as never), error: r.error, loading: r.loading });
