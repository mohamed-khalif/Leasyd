// The two /v1/app routes. The tenant is never sent: the API takes it from
// the signed-in user's token.
import { idToken } from "./auth";
import { getConfig } from "./config";

export type Signal = "logs" | "traces" | "metrics";
export type Where = { field: string; op: string; value?: unknown };
export type Query = {
  signal: Signal; start: string; end: string;
  where?: Where[]; match?: Record<string, string>; services?: string[];
  group_by?: string[]; aggs?: { fn: string; field?: string }[];
  search?: { limit: number }; order?: "asc" | "desc"; limit?: number;
};
export type Result = { columns: string[]; rows: unknown[][]; stats?: Record<string, unknown> };

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await idToken();
  if (!token) throw new ApiError(401, "Your session has ended. Please sign in again.");
  const res = await fetch(getConfig().apiBase + path, {
    ...init, headers: { ...(init?.headers || {}), Authorization: token, "Content-Type": "application/json" },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = (body as { error?: string; message?: string }).error || (body as { message?: string }).message;
    throw new ApiError(res.status, msg || (res.status === 504 ? "The query took too long. Try a shorter time range." : `Request failed (${res.status}).`));
  }
  return body as T;
}

export const me = () => call<{ tenant: string; email: string }>("/v1/app/me");
export const query = (q: Query) => call<Result>("/v1/app/query", { method: "POST", body: JSON.stringify(q) });

/** Rows of a result as objects keyed by column name. */
export function records(r: Result): Record<string, unknown>[] {
  return r.rows.map((row) => Object.fromEntries(r.columns.map((c, i) => [c, row[i]])));
}
