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
  if (!res.ok) throw apiError(res.status, body);
  return body as T;
}

/** POST /v1/app/query. A query still running after ~20 s carries on in the background (202 and a
 *  job id): poll its answer every 2 s, up to 6 minutes. `background` asks for that from the start
 *  (long SQL ranges); onWait hears the seconds waited so far. */
async function runQuery<T>(q: object, opts: { background?: boolean; onWait?: (seconds: number) => void } = {}): Promise<T> {
  const t0 = Date.now();
  let res = await raw("/v1/app/query", { method: "POST", body: JSON.stringify(opts.background ? { ...q, async: true } : q) });
  while (res.status === 202) {
    const job = (res.body as { job?: string }).job;
    if (!job || Date.now() - t0 > 6 * 60_000) throw new ApiError(504, "The query took too long. Try a shorter time range.");
    opts.onWait?.(Math.round((Date.now() - t0) / 1000));
    await new Promise((r) => setTimeout(r, 2000));
    res = await raw(`/v1/app/query/${job}`);
  }
  if (res.status >= 400) throw apiError(res.status, res.body);
  return res.body as T;
}

async function raw(path: string, init?: RequestInit): Promise<{ status: number; body: unknown }> {
  const token = await idToken();
  if (!token) throw new ApiError(401, "Your session has ended. Please sign in again.");
  const res = await fetch(getConfig().apiBase + path, {
    ...init, headers: { ...(init?.headers || {}), Authorization: token, "Content-Type": "application/json" },
  });
  return { status: res.status, body: await res.json().catch(() => ({})) };
}

function apiError(status: number, body: unknown) {
  const msg = (body as { error?: string; message?: string }).error || (body as { message?: string }).message;
  return new ApiError(status, msg || (status === 504 ? "The query took too long. Try a shorter time range." : `Request failed (${status}).`));
}

// PromQL over all signals, and read-only SQL, through the same route.
export type PromSeries = { metric: Record<string, string>; values: [number, string][] };
export type PromResult = { status: string; data: { resultType: string; result: PromSeries[] }; stats?: Record<string, number> };
export const promql = (q: { promql: string; start: string; end: string; step: number }, opts?: Parameters<typeof runQuery>[1]) =>
  runQuery<PromResult>(q, opts);
/** One PromQL evaluation at a moment (epoch seconds): a value per series, e.g. over [$__range]. */
export type PromInstant = { status: string; data: { resultType: string; result: { metric: Record<string, string>; value: [number, string] }[] } };
export const promqlAt = (q: { promql: string; time: number }, opts?: Parameters<typeof runQuery>[1]) => runQuery<PromInstant>(q, opts);
export type SqlResult = { columns: string[]; rows: unknown[][]; truncated?: boolean; stats?: Record<string, number> };
export const sql = (q: { sql: string; start: string; end: string }, opts?: Parameters<typeof runQuery>[1]) =>
  runQuery<SqlResult>(q, opts);

export const me = () => call<{ tenant: string; email: string }>("/v1/app/me");

/** Public: self-service sign-up. The account is made in the background; its owner is emailed. */
export async function signup(email: string, company: string, website = ""): Promise<void> {
  const res = await fetch(getConfig().apiBase + "/v1/signup", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, company, website }) });
  if (!res.ok) throw apiError(res.status, await res.json().catch(() => ({})));
}

// The tenant's own account (Settings): plan and today's data, users, API keys.
export type AccountUser = { email: string; role: "owner" | "member"; created_at?: string; invited_by?: string };
export type AccountKey = { key_id: string; scope: "ingest" | "read"; status: string; created_at?: string; expires_at?: string };
export type Account = {
  tenant: string; company: string; plan: string; daily_cap_bytes?: number | null; created_at?: string;
  today: { bytes: number; records: number; refused_bytes: number };
  searches?: { units_today: number; units_per_day: number | null };
  trial_ends_at?: string | null;
  you: { email: string; role: "owner" | "member" }; users: AccountUser[]; keys: AccountKey[];
  limits: { keys: number; users: number };
};
export const account = {
  get: () => call<Account>("/v1/app/account"),
  invite: (email: string, role: "owner" | "member" = "member") =>
    call<AccountUser>("/v1/app/account/users", { method: "POST", body: JSON.stringify({ email, role }) }),
  remove: (email: string) => call<unknown>(`/v1/app/account/users/${encodeURIComponent(email)}`, { method: "DELETE" }),
  createKey: (scope: "ingest" | "read") =>
    call<{ key_id: string; scope: string; api_key: string }>("/v1/app/account/keys", { method: "POST", body: JSON.stringify({ scope }) }),
  revokeKey: (id: string) => call<unknown>(`/v1/app/account/keys/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
export const query = (q: Query, opts?: Parameters<typeof runQuery>[1]) => runQuery<Result>(q, opts);

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
export type AlertRule = { id: string; name: string; type: "check_failing" | "slo_burn" | "query"; channels: string[]; enabled: boolean;
                          checks?: string[]; failures?: number; slo?: string; burn_rate?: number; budget_below?: number;
                          // check rules (type "query"): a PromQL query and thresholds on each series it returns
                          promql?: string; op?: string; critical?: number; degraded?: number | null; for_minutes?: number;
                          every_minutes?: number; summary?: string | null;
                          firing?: string[]; created_at?: string };
export type RulePreview = { time: number; total: number; series: { labels: Record<string, string>; name: string; value: number; level: "ok" | "degraded" | "critical" }[] };
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
  preview: (r: { promql: string; op: string; critical?: number; degraded?: number | null }) =>
    call<RulePreview>("/v1/app/alerts/rules/preview", json("POST", r)),
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

// Dashboards (/v1/app/dashboards): panels of PromQL queries; the same for everyone in the tenant.
export type PanelType = "timeseries" | "bars" | "stat" | "text";
export type Panel = { id: string; type: PanelType; title: string; description?: string; w: number; h: number;
                      queries?: { promql: string; legend?: string }[]; unit?: string; decimals?: number; text?: string };
export type Dashboard = { id: string; name: string; description: string; variables: { name: string; label: string; field?: string }[]; panels: Panel[];
                          version: number; updated_at?: string; updated_by?: string; created_by?: string; builtin?: boolean };
export type DashboardSummary = { id: string; name: string; description: string; panels: number; version: number; updated_at?: string; updated_by?: string };
export const dashboards = {
  list: () => call<{ items: DashboardSummary[]; limit: number }>("/v1/app/dashboards"),
  get: (id: string) => call<Dashboard>(`/v1/app/dashboards/${id}`),
  create: (d: Omit<Dashboard, "id" | "version">) => call<Dashboard>("/v1/app/dashboards", json("POST", d)),
  update: (id: string, d: Partial<Dashboard> & { version: number }) => call<Dashboard>(`/v1/app/dashboards/${id}`, json("PUT", d)),
  remove: (id: string) => call<{ deleted: string }>(`/v1/app/dashboards/${id}`, json("DELETE")),
};

// The AI SRE: ask a question; it investigates in the background (poll the conversation while "running").
export type AiStep =
  | { type: "question"; text: string; by?: string; at?: string }
  | { type: "progress" | "note" | "answer" | "error"; text: string }
  | { type: "tool"; name: string; input: Record<string, unknown>; summary?: string; error?: string };
export type AiConversation = { id: string; title: string; status: "running" | "done" | "failed"; created_by: string; created_at: string;
                               updated_at: string; view: AiStep[]; range?: [string, string] };
export const ai = {
  list: () => call<{ conversations: { id: string; title: string; status: string; updated_at: string }[]; enabled: boolean }>("/v1/app/ai/conversations"),
  get: (id: string) => call<AiConversation>(`/v1/app/ai/conversations/${encodeURIComponent(id)}`),
  ask: (message: string, opts: { conversation_id?: string; start?: string; end?: string; page?: unknown } = {}) =>
    call<AiConversation>("/v1/app/ai/conversations", { method: "POST", body: JSON.stringify({
      message, ...(opts.conversation_id ? { conversation_id: opts.conversation_id } : {}),
      context: { start: opts.start, end: opts.end, ...(opts.page ? { page: opts.page } : {}) } }) }),
};
