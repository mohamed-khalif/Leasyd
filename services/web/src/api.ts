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

// Synthetic checks (/v1/app/checks): the signed-in user's tenant's own checks.
export type Constraint = { type: string; expr?: string; value?: string | number; name?: string; path?: string; op?: string };
export type Extraction = { name: string; from: "json" | "regex" | "header"; expr: string };
export type Step = {
  name: string; method: string; url: string; headers: Record<string, string>; body?: string;
  auth: { type: "none" | "basic" | "bearer"; username?: string; password?: string; token?: string };
  user_agent?: string; follow_redirects: boolean; verify_tls: boolean; record_body: boolean;
  extract: Extraction[]; constraints: Constraint[];
};
export type CheckSettings = {
  name: string; frequency: number; timeout_ms: number; enabled: boolean;
  variables: Record<string, string>; steps: Step[];
  secrets?: Record<string, string | null>;    // write-only: new values, or null to remove
};
export type Check = Omit<CheckSettings, "secrets"> & { id: string; secret_names: string[]; created_at: string; updated_at: string; created_by?: string };
export type Timings = { dns_ms?: number; connect_ms?: number; tls_ms?: number; ttfb_ms?: number; total_ms?: number };
export type StepResult = { name: string; ok: boolean; failure: string | null; status: number | null; url: string; timings: Timings;
                           tls_days?: number | null; extracted: string[]; body_sample?: string | null };
export type CheckResult = { ok: boolean; failure: string | null; failed_step: number | null; total_ms: number;
                            tls_days?: number | null; steps: StepResult[] };
const json = (method: string, body?: unknown): RequestInit => ({ method, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
export const checks = {
  list: () => call<{ checks: Check[]; limit: number }>("/v1/app/checks"),
  get: (id: string) => call<Check>(`/v1/app/checks/${encodeURIComponent(id)}`),
  create: (c: CheckSettings) => call<Check>("/v1/app/checks", json("POST", c)),
  update: (id: string, c: Partial<CheckSettings>) => call<Check>(`/v1/app/checks/${encodeURIComponent(id)}`, json("PUT", c)),
  remove: (id: string) => call<{ deleted: string }>(`/v1/app/checks/${encodeURIComponent(id)}`, json("DELETE")),
  run: (id: string) => call<{ result: CheckResult }>(`/v1/app/checks/${encodeURIComponent(id)}/run`, json("POST")),
  test: (c: CheckSettings & { id?: string }) => call<{ result: CheckResult }>("/v1/app/checks/test", json("POST", c)),
};

/** Rows of a result as objects keyed by column name. */
export function records(r: Result): Record<string, unknown>[] {
  return r.rows.map((row) => Object.fromEntries(r.columns.map((c, i) => [c, row[i]])));
}
