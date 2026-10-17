// fetch answered by the mock backend (mock/mockApi.ts, the one `npm run mock` serves), so page
// tests render against the same data as the local preview. Kubernetes and GPU SQL needs
// mock/engine_server.py; without it those panels show their error state, which is fine here.
import { mockApi } from "../../mock/mockApi";
import { loadConfig, getConfig } from "../config";

type Next = () => void;
type Middleware = (req: unknown, res: unknown, next: Next) => void;

const middlewares: Middleware[] = [];
(mockApi() as unknown as { configureServer: (s: unknown) => void })
  .configureServer({ middlewares: { use: (m: Middleware) => middlewares.push(m) } });

export let inFlight = 0;
export const requests: string[] = [];
/** "METHOD /path" (no query string) -> the status to fail it with, for error-path tests. */
export const failing = new Map<string, number>();

function serve(method: string, url: string, body: string): Promise<Response> {
  return new Promise((resolve) => {
    const listeners: Record<string, ((x?: unknown) => void)[]> = {};
    const req = {
      method, url, headers: {},
      on(event: string, cb: (x?: unknown) => void) {
        (listeners[event] ??= []).push(cb);
        if (event === "end") setTimeout(() => { listeners.data?.forEach((f) => body && f(body)); cb(); });
        return req;
      },
    };
    const headers: Record<string, string> = {};
    const res = {
      statusCode: 200,
      setHeader: (k: string, v: string) => { headers[k] = v; },
      end: (text = "") => resolve(new Response(text || null, { status: res.statusCode, headers })),
    };
    let i = 0;
    const next = () => {
      const m = middlewares[i++];
      if (m) m(req, res, next);
      else { res.statusCode = 404; res.end(JSON.stringify({ error: `no mock for ${method} ${url}` })); }
    };
    next();
  });
}

export async function installMockBackend() {
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://app.test");
    if (url.pathname === "/config.json")
      return new Response(JSON.stringify({ region: "us-east-1", userPoolId: "us-east-1_TEST", clientId: "test", apiBase: "", mock: true }));
    if (!url.pathname.startsWith("/v1/")) return new Response("{}", { status: 404 });
    const method = init?.method ?? "GET";
    requests.push(`${method} ${url.pathname}`);
    const fail = failing.get(`${method} ${url.pathname}`);
    if (fail) return new Response(JSON.stringify({ error: `forced ${fail}` }), { status: fail });
    inFlight++;
    try {
      return await serve(method, url.pathname + url.search, typeof init?.body === "string" ? init.body : "");
    } finally {
      inFlight--;
    }
  }) as typeof fetch;
  await loadConfig();
  getConfig().mock = true;
}
