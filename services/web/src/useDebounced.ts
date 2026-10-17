import { useCallback, useEffect, useRef, useState } from "react";

/** Typing waits this long after the last key before a query runs; Enter or Run doesn't wait. */
export const TYPING_MS = 400;

/** `value`, settled: it follows `value` once it has stopped changing for `ms`. flush() takes the
 *  current value at once (Enter, Run). */
export function useDebounced<T>(value: T, ms = TYPING_MS): [T, () => void] {
  const [settled, setSettled] = useState(value);
  const latest = useRef(value);
  useEffect(() => {
    latest.current = value;
    const t = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  const flush = useCallback(() => setSettled(latest.current), []);
  return [settled, flush];
}
