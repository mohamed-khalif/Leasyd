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
