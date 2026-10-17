// Dev only (`npm run dev` / `npm run mock`; compiled out of production builds): records every fetch
// the app makes, for measuring requests per page, duplicates, latency and response size. Read it
// from the browser console or a test: window.__leasydRequests, window.__leasydRequestsReset().
export type DevRequest = { method: string; path: string; body: string; status: number; ms: number; bytes: number; at: number };

declare global {
  interface Window { __leasydRequests?: DevRequest[]; __leasydRequestsReset?: () => void }
}

if (import.meta.env.DEV) {
  const log: DevRequest[] = [];
  window.__leasydRequests = log;
  window.__leasydRequestsReset = () => { log.length = 0; };
  const original = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const at = performance.now();
    const res = await original(input, init);
    const bytes = Number(res.headers.get("content-length")) || (await res.clone().arrayBuffer()).byteLength;
    log.push({ method: init?.method ?? "GET", path: new URL(url, location.href).pathname, body: typeof init?.body === "string" ? init.body : "",
               status: res.status, ms: performance.now() - at, bytes, at });
    return res;
  };
}
