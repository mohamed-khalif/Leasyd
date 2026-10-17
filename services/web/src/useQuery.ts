import { useEffect, useState } from "react";
import { query, Query, Result, sql } from "./api";

export type Load<T> = { data: T | null; error: string | null; loading: boolean };
export type Loaded = Load<Result>;

/** The one data hook: runs `run` when `key` changes (null: runs nothing and keeps the state).
 *  While a new key loads, the last data stays; an error clears it. A new key or unmounting aborts
 *  the request in flight (`signal`; a running job is cancelled too), so its answer is never used.
 *  `loading` starts as `initialLoading` (default: whether there is anything to run). */
export function useLoad<T>(run: ((signal: AbortSignal) => Promise<T>) | null, key: string, initialLoading = !!run): Load<T> {
  const [state, setState] = useState<Load<T>>({ data: null, error: null, loading: initialLoading });
  useEffect(() => {
    if (!run) return;
    const ctl = new AbortController();
    setState((s) => ({ ...s, loading: true, error: null }));
    run(ctl.signal).then(
      (data) => !ctl.signal.aborted && setState({ data, error: null, loading: false }),
      (e: Error) => !ctl.signal.aborted && setState({ data: null, error: e.message, loading: false }),
    );
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed: the caller's `key` says when `run` is a new request
  }, [key]);
  return state;
}

/** Runs a query when `key` changes (the query and the refresh tick). */
export function useQuery(q: Query | null, key: string): Loaded {
  return useLoad(q && ((signal) => query(q, { signal })), key);
}

/** Runs one SQL query (over the window) when `key` changes; null runs nothing. */
export function useSql(text: string | null, w: { start: string; end: string }, key: string): Loaded {
  return useLoad(text !== null && text !== "" ? (signal) => sql({ sql: text, ...w }, { signal }) : null, key);
}
