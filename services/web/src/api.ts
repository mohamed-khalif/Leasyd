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
  collapse?: number;   // count the groups under the first n group_by columns (e.g. series per metric)
};
export type Result = { columns: string[]; rows: unknown[][]; stats?: Record<string, unknown>; truncated?: boolean };

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

// PromQL over all signals, and read-only SQL, through the same route.
export type PromSeries = { metric: Record<string, string>; values: [number, string][] };
export type PromResult = { status: string; data: { resultType: string; result: PromSeries[] }; stats?: Record<string, number> };
export const promql = (q: { promql: string; start: string; end: string; step: number }) =>
  call<PromResult>("/v1/app/query", { method: "POST", body: JSON.stringify(q) });
export type SqlResult = { columns: string[]; rows: unknown[][]; truncated?: boolean; stats?: Record<string, number> };
export const sql = (q: { sql: string; start: string; end: string }) =>
  call<SqlResult>("/v1/app/query", { method: "POST", body: JSON.stringify(q) });

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
export type BrowserAction = "navigate" | "click" | "hover" | "type" | "select" | "press" | "wait_for" | "wait"
  | "assert_text" | "assert_no_text" | "assert_element" | "assert_url" | "extract";
export type BrowserStep = {
  name: string; action: BrowserAction; url?: string; selector?: string; text?: string; value?: string;
  key?: string; ms?: number; variable?: string; attribute?: string; timeout_ms?: number;
};
export type CheckSettings = {
  type?: "http" | "browser";
  name: string; frequency: number; timeout_ms: number; enabled: boolean;
  variables: Record<string, string>; steps: Step[] | BrowserStep[];
  device?: "desktop" | "mobile"; screenshots?: "failure" | "every_step"; verify_tls?: boolean;   // browser checks
  secrets?: Record<string, string | null>;    // write-only: new values, or null to remove
};
export type Exclusion = { check: string; run_id: string; reason: string; by?: string; at: string };
export type Check = Omit<CheckSettings, "secrets"> & { id: string; secret_names: string[]; created_at: string; updated_at: string; created_by?: string;
                                                       excluded_runs?: string[]; exclusions?: Exclusion[] };
export type Timings = { dns_ms?: number; connect_ms?: number; tls_ms?: number; ttfb_ms?: number; total_ms?: number };
export type Vitals = { ttfb_ms: number | null; fcp_ms: number | null; lcp_ms: number | null; cls: number | null;
                       dom_ms: number | null; load_ms: number | null; transfer_bytes: number | null };
export type StepResult = { name: string; ok: boolean; failure: string | null; status: number | null; url: string; timings: Timings;
                           tls_days?: number | null; extracted: string[]; body_sample?: string | null;
                           // browser steps
                           action?: BrowserAction; vitals?: Vitals | null; console_errors?: string[]; http_errors?: string[];
                           failed_requests?: string[]; blocked?: string[]; screenshot?: string | null };   // screenshot: base64 JPEG
export type CheckResult = { ok: boolean; failure: string | null; failed_step: number | null; total_ms: number;
                            tls_days?: number | null; steps: StepResult[]; run_id?: string };
const json = (method: string, body?: unknown): RequestInit => ({ method, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
export const checks = {
  list: () => call<{ checks: Check[]; limit: number }>("/v1/app/checks"),
  get: (id: string) => call<Check>(`/v1/app/checks/${encodeURIComponent(id)}`),
  create: (c: CheckSettings) => call<Check>("/v1/app/checks", json("POST", c)),
  update: (id: string, c: Partial<CheckSettings>) => call<Check>(`/v1/app/checks/${encodeURIComponent(id)}`, json("PUT", c)),
  remove: (id: string) => call<{ deleted: string }>(`/v1/app/checks/${encodeURIComponent(id)}`, json("DELETE")),
  run: (id: string) => call<{ result: CheckResult }>(`/v1/app/checks/${encodeURIComponent(id)}/run`, json("POST")),
  test: (c: CheckSettings & { id?: string }) => call<{ result: CheckResult }>("/v1/app/checks/test", json("POST", c)),
  exclude: (id: string, run_id: string, reason: string) =>
    call<Exclusion>(`/v1/app/checks/${encodeURIComponent(id)}/exclusions`, json("POST", { run_id, reason })),
  include: (id: string, run: string) => call<{ included: string }>(`/v1/app/checks/${encodeURIComponent(id)}/exclusions/${run}`, json("DELETE")),
  screenshot: (id: string, run: string, step: number) =>
    call<{ image: string; content_type: string }>(`/v1/app/checks/${encodeURIComponent(id)}/screenshot?run=${encodeURIComponent(run)}&step=${step}`),
};

// Maintenance windows (/v1/app/windows): runs of the chosen checks inside one are recorded as excluded.
export type WindowSchedule = { type: "once"; start: string; end: string }
  | { type: "weekly"; days: string[]; start: string; duration_minutes: number; timezone: string };
export type MaintenanceWindow = { id: string; name: string; checks: string[]; schedule: WindowSchedule; created_at?: string; created_by?: string };
// SLOs (/v1/app/slos) over synthetic checks, evaluated from their results.
export type Slo = { id: string; name: string; description: string; type: "availability" | "performance"; checks: string[];
                    target: number; window_days: number; threshold_ms?: number; created_at?: string; updated_at?: string; created_by?: string };
function settingsApi<T extends { id: string }>(base: string) {
  return {
    list: () => call<{ items: T[]; limit: number }>(base),
    get: (id: string) => call<T>(`${base}/${encodeURIComponent(id)}`),
    create: (x: Omit<T, "id">) => call<T>(base, json("POST", x)),
    update: (id: string, x: Partial<T>) => call<T>(`${base}/${encodeURIComponent(id)}`, json("PUT", x)),
    remove: (id: string) => call<{ deleted: string }>(`${base}/${encodeURIComponent(id)}`, json("DELETE")),
  };
}
export const windows = settingsApi<MaintenanceWindow>("/v1/app/windows");
export const slos = settingsApi<Slo>("/v1/app/slos");

// Alerts (/v1/app/alerts/...): where they go (channels) and when (rules).
export type AlertChannel = { id: string; name: string; type: "email" | "slack" | "webhook"; email?: string; url_hint?: string;
                             status?: string; signing_secret?: string; created_at?: string };
export type AlertRule = { id: string; name: string; type: "check_failing" | "slo_burn"; channels: string[]; enabled: boolean;
                          checks?: string[]; failures?: number; slo?: string; burn_rate?: number; budget_below?: number;
                          firing?: string[]; created_at?: string };
export const alerts = {
  channels: () => call<{ items: AlertChannel[]; limit: number }>("/v1/app/alerts/channels"),
  addChannel: (c: { type: string; name: string; email?: string; url?: string }) => call<AlertChannel>("/v1/app/alerts/channels", json("POST", c)),
  removeChannel: (id: string) => call<{ deleted: string }>(`/v1/app/alerts/channels/${id}`, json("DELETE")),
  testChannel: (id: string) => call<{ sent: boolean; error: string | null }>(`/v1/app/alerts/channels/${id}/test`, json("POST", {})),
  rules: () => call<{ items: AlertRule[]; limit: number }>("/v1/app/alerts/rules"),
  rule: (id: string) => call<AlertRule>(`/v1/app/alerts/rules/${id}`),
  addRule: (r: Omit<AlertRule, "id">) => call<AlertRule>("/v1/app/alerts/rules", json("POST", r)),
  updateRule: (id: string, r: Partial<AlertRule>) => call<AlertRule>(`/v1/app/alerts/rules/${id}`, json("PUT", r)),
  removeRule: (id: string) => call<{ deleted: string }>(`/v1/app/alerts/rules/${id}`, json("DELETE")),
};

/** Where-conditions that leave excluded runs out: maintenance windows, and runs excluded by hand. */
export function notExcluded(runIds: string[] = []): Where[] {
  return [{ field: "attributes.check.excluded", op: "not_exists" },
          ...(runIds.length ? [{ field: "attributes.check.run_id", op: "not_in", value: runIds }] : [])];
}

/** Rows of a result as objects keyed by column name. */
export function records(r: Result): Record<string, unknown>[] {
  return r.rows.map((row) => Object.fromEntries(r.columns.map((c, i) => [c, row[i]])));
}
