import { useEffect, useState } from "react";
import { query, Query, Result, sql } from "./api";

export type Loaded = { data: Result | null; error: string | null; loading: boolean };

/** Runs a query when `key` changes (the query and the refresh tick). */
export function useQuery(q: Query | null, key: string): Loaded {
  const [state, setState] = useState<Loaded>({ data: null, error: null, loading: !!q });
  useEffect(() => {
    if (!q) return;
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    query(q).then(
      (data) => live && setState({ data, error: null, loading: false }),
      (e: Error) => live && setState({ data: null, error: e.message, loading: false }),
    );
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

/** Runs one SQL query (over the window) when `key` changes; null runs nothing. */
export function useSql(text: string | null, w: { start: string; end: string }, key: string): Loaded {
  const [state, setState] = useState<Loaded>({ data: null, error: null, loading: !!text });
  useEffect(() => {
    if (!text) return;
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    sql({ sql: text, ...w }).then((data) => live && setState({ data, error: null, loading: false }),
      (e: Error) => live && setState({ data: null, error: e.message, loading: false }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}
