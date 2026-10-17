// A fetch that answers from a function, records every request, and serves /config.json, so
// api.ts runs unchanged in tests. install() also loads the app's config through it (mock mode:
// the token is "mock-token", no Cognito).
import { loadConfig, getConfig } from "../config";

export type Call = { method: string; path: string; body: unknown; headers: Record<string, string>; signal?: AbortSignal };
export type Answer = { status: number; body?: unknown } | Error;
type Handler = (call: Call) => Answer | Promise<Answer>;

export const calls: Call[] = [];
let handler: Handler = () => ({ status: 404, body: { error: "no handler" } });

export function answer(h: Handler) { handler = h; calls.length = 0; }

export async function install() {
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://app.test");
    if (url.pathname === "/config.json")
      return new Response(JSON.stringify({ region: "us-east-1", userPoolId: "us-east-1_TEST", clientId: "test", apiBase: "", mock: true }));
    const call = { method: init?.method ?? "GET", path: url.pathname + url.search,
                   body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
                   headers: (init?.headers ?? {}) as Record<string, string>, signal: init?.signal ?? undefined };
    calls.push(call);
    // Like fetch: an aborted signal rejects with an AbortError, before or while waiting for the answer.
    const aborted = () => new DOMException("The operation was aborted.", "AbortError");
    if (init?.signal?.aborted) throw aborted();
    const a = await (init?.signal
      ? Promise.race([handler(call), new Promise<never>((_, no) => init.signal!.addEventListener("abort", () => no(aborted()), { once: true }))])
      : handler(call));
    if (a instanceof Error) throw a;
    return new Response(a.body === undefined ? "" : typeof a.body === "string" ? a.body : JSON.stringify(a.body), { status: a.status });
  }) as typeof fetch;
  await loadConfig();
  getConfig().mock = true;
}
