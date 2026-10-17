import { useCallback, useEffect, useState } from "react";
// The hash URL is "#/path?a=1&b=2". Pages and the app keep state there (the time range, filters)
// with replaceState, so it can be shared and Back restores it, without a hashchange per edit.

/** The query parameters of a hash ("#/path?..." or "/path?..."). */
export function hashParams(hash = location.hash): URLSearchParams {
  return new URLSearchParams(hash.split("?")[1] ?? "");
}

/** Set (or, with null, remove) parameters of the current hash URL without a new history entry. */
export function replaceHashParams(updates: Record<string, string | null>) {
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const p = new URLSearchParams(query ?? "");
  for (const [k, v] of Object.entries(updates)) {
    if (v === null) p.delete(k);
    else p.set(k, v);
  }
  const qs = p.toString();
  history.replaceState(history.state, "", `#${path}${qs ? `?${qs}` : ""}`);
}

/** A page filter kept in the link: starts from it, is written back (replaceState) when set, and is
 *  read again when a link or Back changes it. The fallback is left out of the link. */
export function useHashParam(name: string, fallback = ""): [string, (v: string) => void] {
  const [value, setValue] = useState(() => hashParams().get(name) ?? fallback);
  useEffect(() => {
    const on = () => setValue(hashParams().get(name) ?? fallback);
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, [name, fallback]);
  const set = useCallback((v: string) => {
    setValue(v);
    replaceHashParams({ [name]: v === fallback ? null : v });
  }, [name, fallback]);
  return [value, set];
}

/** A search box kept in the link: `applied` (on submit) is the link's `name`; `text` is what is
 *  typed, reset to the link's value when a link or Back changes it. */
export function useHashSearch(name: string) {
  const [applied, setApplied] = useHashParam(name);
  const [text, setText] = useState(applied);
  const [seen, setSeen] = useState(applied);
  if (seen !== applied) {               // adjusting state on a change, during render
    setSeen(applied);
    setText(applied);
  }
  return { text, setText, applied, setApplied };
}
