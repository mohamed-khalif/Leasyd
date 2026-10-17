# Design: query lifecycle, limits and cancellation

Status: **proposal for review** (2026-10-17). It covers the security items from the code-health review that you
asked to see before they are built:
- the end of the double run;
- backend cancellation;
- the per-tenant concurrency cap;
- fail-closed limits;
- the password-flow migration.

Paths are relative to `services/compaction` unless a section says otherwise.

Already built and committed (no design needed):

| Item | Commit | What it does |
|---|---|---|
| SQL checker refuses over-nested queries | `a9ea195` | Returns 400 instead of crashing the Lambda |
| SQL checker scopes CTE names | `f2aad5c` | Closes the bypass |
| Query timeout | `4dbd952` | DuckDB is interrupted at `QUERY_TIMEOUT_S`, 240 s, in SQL and engine workers |
| Reserved concurrency | `0ea2294` | See §1. **Not deployed yet.** |

---

## 1. Reserved concurrency: checked against ingest

In Lambda, a reservation both **guarantees** concurrency and **caps** the function at that number. So the
risk you flagged is real in both directions:
- if ingest's reservation is too small, ingest is throttled by its own cap;
- if queries are uncapped, they can starve ingest.

**Measured (CloudWatch, 60 days, which includes the soak and stress tests):**

| Item | Value |
|---|---|
| Account limit | 10,000 concurrent |
| Account peak | 868 |
| Throttles | 64 in 60 days, all from the fault-injection tests |

| Function | 60-day peak | p99 duration | Concurrency at the stage maximum of 5,000 req/s × p99 | Setting |
|---|---|---|---|---|
| obs-ingest | 79 | 157 ms | 785 | **reserve 1,000** |
| obs-ingest-authorizer | 55 | 133 ms (results cached 60 s) | 665 | **reserve 800** |
| obs-query-worker | 849 | 1.0 s p95 | – | **cap 1,000** |
| obs-query-api | 28 | 2.7 s p95 | – | **cap 300** |
| obs-query (alerts, AI, MCP) | 8 | – | – | **cap 100** |

**Why ingest can't be throttled by this:**
- API Gateway itself refuses traffic above 5,000 req/s for the whole stage (`phaseT2-ingest.yaml`), so ingest can't receive more than that.
- At p99 latency that rate needs 785 concurrent executions, and ingest has 1,000 reserved. Ingest's own reservation can't throttle it below the gateway's limit.
- Queries can no longer take ingest's share.

**Total reserved:** 3,200 of 10,000. Everything else (compaction, fast lane, alerts, synthetics) keeps 6,800 unreserved. Their peaks are 133 and 117.

**Caveats:**
- A **new account with the default limit of 1,000** can't hold these reservations (AWS keeps at least 100 unreserved). The parameters accept 0, meaning "none", for such accounts. This is documented in the templates.
- Deploying needs `lambda:PutFunctionConcurrency` and `lambda:DeleteFunctionConcurrency`. The CloudShell admin has them; the `obs-deployer` user may not.
- **After deploy, check:** `Throttles` on obs-ingest stays 0. The existing `obs-lambda-throttles` alarm, account-wide at ≥10 in 5 min, already watches this. I'd add a dedicated `obs-ingest-throttled` alarm (≥1 in 5 min) in the same change.

---

## 2. Ending the double run past 20 seconds

**Today** (`query.py:805-816`): `obs-query-api` runs the query in a thread. At 20 s it **starts the same query
again** as an async job and returns 202. The first run keeps going: up to 64 workers × 300 s. Units are charged twice.

**Options considered:**

| Option | How | Verdict |
|---|---|---|
| A | Decide sync or async up front from the plan's byte count | Estimates are wrong both ways; borderline queries still need a fallback |
| B | Every query is a job; the API waits up to 20 s on S3 for its answer | Adds an async-invoke hop and S3 polling to every query (+100–300 ms on today's p50 of 400 ms) |
| **C (proposed)** | The API invokes the coordinator `obs-query` **synchronously**, with a 20 s read timeout and the job id in the payload | See below |

**How option C works:**
1. The coordinator always writes its answer to `_results/jobs/<tenant>/<job>.json`, then returns it.
2. If the answer arrives within 20 s, the API returns it inline, as today.
3. If the read times out, the API writes `{"status":"running"}` *only if no answer exists yet* (S3 conditional write, `If-None-Match: *`) and returns 202 with the job id.
4. Lambda does not stop an invocation when the caller disconnects, so **the same run** finishes and stores its answer. There is no second run, no duplicate charge, and fast queries have no extra polling.

**Details:**
- **Client config:** the boto3 client for this call uses `read_timeout=20, retries={"max_attempts": 0}`. A retry here would start a second run.
- **The one-chunk shortcut** (today the API runs single-chunk queries itself) moves into the coordinator. Cost: one Lambda-to-Lambda hop, about 20–40 ms warm.
- **Charging:** units are charged once, by the coordinator, per job id.
- `async: true` (the SQL page, ≥ 24 h ranges) becomes the same call with read timeout 0, which returns 202 straight away.
- **Coordinator memory:** it already has 2 GB and 300 s. It now runs the API's share of queries, so the obs-query cap in §1 rises to the API's 300.
- **Direct invocations** (alerts, AI, MCP) keep calling the coordinator as today.

**Risks:**
- Cold starts of obs-query add latency until it is warm.
- The 300 s coordinator timeout becomes the hard ceiling for every query, as it already is for jobs.

---

## 3. Per-tenant concurrency cap

**Goal:** one tenant can't hold more than N queries at once. That bounds the 64-worker fan-out per tenant and keeps the worker cap shared fairly.

**Proposed limits:**

| Plan | Queries in flight |
|---|---|
| free | 4 |
| standard | 12 |

A query counts from coordinator start to finish, including jobs. The Home page sends 20 at once but they finish in about 1–3 s, so see the note on the browser side below.

**Store:** one item per tenant in `obs-tenants`: `pk = inflight#<tenant>`, holding a map `leases: {<job id>: <expires epoch>}`.
- **Acquire:** one `UpdateItem`: `SET leases.#id = :exp` with `ConditionExpression: size(leases) < :max` (or the attribute is missing).
  - Expired entries are pruned first in the same call path: one `UpdateItem REMOVE` of the expired ids read from the last value.
  - On ConditionalCheckFailed the API answers **429 "too many queries running at once"** with `Retry-After: 2`.
- **Release:** `REMOVE leases.#id` in `finally`.
- **Self-healing:** each lease expires at start + 300 s (the Lambda timeout), so a crash or kill can't leak a slot for longer than that.
- **Cost:** 2 writes per query (about $2.50 per million queries).

**Browser side:** the Home page's 20 parallel queries would hit a cap of 4 or 12. So the client gets a per-tab limit on concurrent queries, the same idea as Dashboards' existing 6-slot limiter, generalised in the core hook (Phase 6). Until then, the cap is enforced but set to 25 for both plans. It then tightens to 4 and 12 when the client limiter ships, so the change doesn't cause 429s on Home.

---

## 4. Cancellation

### 4.1 Client

- `runQuery(q, {signal})` takes an `AbortSignal`, passed to `fetch` and to the 2 s poll sleep.
- The core hook aborts on unmount and on key change.
- When it aborts after a 202, the client sends `DELETE /v1/app/query/{job}` (fire-and-forget) and stops polling immediately.
- Dashboards' limiter releases a slot on abort.
- Sync requests (before any 202) can only be dropped: API Gateway doesn't tell the Lambda the client has gone. They are bounded by the 20 s handover plus the timeout.

### 4.2 Backend

- **Routes** `DELETE /v1/app/query/{job}` (Cognito) and `DELETE /v1/query/{job}` (API key, read scope), handled by `obs-query-api`:
  - The tenant comes from the authorizer, as for GET, and the job id must match `^[0-9a-f]{32}$`.
  - It writes `_results/jobs/<tenant>/<job>.cancel` (empty object) and answers 202 `{"status": "cancelling"}`.
  - It is idempotent. An unknown job id gives 404; a finished job gives 409 `{"status": "done"}`.
- **Coordinator:** checks for the cancel marker before the fan-out and before each new batch of workers.
  - If the marker is there, it stops scheduling, releases the lease and stores `{"status": "cancelled"}`.
  - GET of the job then answers 410 Gone, `{"error": "the query was cancelled"}`.
- **Workers:** each gets `{"job": <id>, "tenant": …}` in its payload. The `_Deadline` thread that now interrupts DuckDB at 240 s also checks for the cancel marker every 5 s (one S3 HEAD), and interrupts DuckDB when it appears.
  - Cost: at most 48 HEADs per worker-minute, about $0.02 per 1,000 worker-minutes.
  - Workers without a job id (sync queries) skip the check.
- **Charging:** units for the work done so far are still charged once.
- **IAM:** none new. The query role already has `_results/*`.
- **Lifecycle:** markers expire with `_results/` after 1 day.
- **Tests:**
  - cancel before the fan-out, so no workers start;
  - cancel mid-run, so a worker is interrupted within about 5 s and the stored status is "cancelled";
  - cancel after done gives 409;
  - another tenant's job id gives 404;
  - a GET after cancel gives 410.

**Why markers instead of a status field in the job object:**
- No read-modify-write race with the worker writing its answer.
- The worker check is a cheap HEAD.

**Reversibility:** the routes are additive, and clients that never call DELETE behave exactly as today. Rolling back means removing the routes and the HEAD check; there is no data migration.

---

## 5. Fail closed, with last-known-good

**Today:** `over_limit` fails **open**. Any DynamoDB error other than the condition check allows the query, so the limits disappear exactly when the system is under stress.

**Proposal:** each check keeps a small last-known-good (LKG) state per container (module-level, per tenant), and decides like this:

| Check | On success | On DynamoDB error, LKG younger than the TTL | On error, no LKG or LKG too old |
|---|---|---|---|
| Per-minute rate | Store `(minute, count)` | Count locally: LKG count + local calls since; allow under the plan limit | **Refuse: 503** `Retry-After: 5`, "limits can't be checked right now" |
| Daily units | Store `(day, used)` | Allow if LKG used + local uncharged units < allowance | Refuse: 503 |
| In-flight leases (§3) | – | Allow up to the plan cap counted **per container** (local in-flight) | Refuse: 503 |
| Plan lookup | Already cached 5 min | Use the cached plan | Refuse: 503 |

**TTLs:**
- **60 s** for the rate and lease checks.
- **10 min** for daily units, which only change slowly.

So a blip of up to a minute in the limits store costs nothing, and an outage longer than that fails closed.

**Notes:**
- `obs-query-api` runs many containers, so local counting is approximate. Worst case during a blip, a tenant gets roughly (containers × its limit) for up to 60 s. That's acceptable for a minute and bounded by the reserved concurrency caps in §1.
- **Logging:** each fallback decision logs `{"limits_fallback": kind, "decision": …}`. A metric filter plus an alarm fires on more than 0 refusals in 5 minutes, so an outage pages instead of silently refusing.
- **MCP** (`ai/mcp.py:157`) uses the same helper, moved to a small shared module so both paths behave the same.
- **Tests:**
  - DynamoDB error with fresh LKG: allowed, counted locally;
  - error with stale LKG: 503;
  - error with no LKG: 503;
  - recovery replaces the LKG.

---

## 6. Password sign-in flow: what depends on it

The public app client `obs-app` allows `ALLOW_USER_SRP_AUTH`, `ALLOW_USER_PASSWORD_AUTH` and `ALLOW_REFRESH_TOKEN_AUTH` (`infra/state.yaml:231`).

| Dependent | Flow | Effect of removing `USER_PASSWORD_AUTH` |
|---|---|---|
| **Web app** (`services/web/src/auth.ts`) | SRP: `authenticateUser` with the library default; no `setAuthenticationFlowType`. Refresh through `REFRESH_TOKEN_AUTH` | None |
| **Existing users** | Same passwords; SRP | None. No password reset needed |
| **obs-canary app-route check** (`services/canary/canary.py:167-170`, every 5 min) | `USER_PASSWORD_AUTH` as `canary-app@leasyd.com`, password in SSM `/obs/canary/app-password` | **Breaks**: the `obs-app-routes` alarm would fire constantly |
| **`infra/login-test.py`** (QA end-to-end) | `USER_PASSWORD_AUTH` and `REFRESH_TOKEN_AUTH` | Breaks |
| MCP, `/v1/query`, collectors, `infra/*.sh` | API keys, or IAM admin APIs (`admin_create_user`, `admin_set_user_password` in `tenant.sh`/`admin.py`) | None |
| CLI | There is no Leasyd CLI that signs in | – |

**Migration plan** (a separate change, after this work):
1. Add a second app client, `obs-ops`:
   - confidential (with a secret);
   - `ExplicitAuthFlows: [ALLOW_ADMIN_USER_PASSWORD_AUTH, ALLOW_REFRESH_TOKEN_AUTH]`;
   - its secret in SSM.
   - `ADMIN_USER_PASSWORD_AUTH` needs IAM credentials (`cognito-idp:AdminInitiateAuth`), so it can't be used from the internet.
2. Move the canary and `login-test.py` to `admin_initiate_auth` with `SECRET_HASH` on `obs-ops`. Give the canary role `cognito-idp:AdminInitiateAuth` on the pool.
3. **Verify** that the API Gateway Cognito authorizer accepts ID tokens from `obs-ops`. It validates the pool, not the client, unless scopes are configured. Check this on AWS before step 4.
4. Remove `ALLOW_USER_PASSWORD_AUTH` from `obs-app` (an in-place update of the app client; it doesn't replace it, and sessions survive).
5. Watch `obs-app-routes` for a day.

**Rollback:** re-add the flow (one template line).

**Optional MFA** (approved as a separate change):
- `MfaConfiguration: OPTIONAL` with TOTP only (no SMS: cost and SIM-swap risk).
- A "Set up two-step sign-in" section in Settings: `associateSoftwareToken` → QR code → `verifySoftwareToken` → `setUserMfaPreference`.
- The sign-in screen handles the `totpRequired` challenge.
- The canary and QA users keep MFA off.

Switching the pool from OFF to OPTIONAL doesn't affect existing users.

---

## 7. Order of work, once approved

| Step | What | Commits |
|---|---|---|
| 1 | §2 (no double run) | One |
| 2 | §3 (leases at 25 in flight) | One |
| 3 | §5 (fail closed with LKG) | One |
| 4 | §4.2 (backend cancel routes, coordinator and worker checks) | One |

- Each step comes with tests, in `test_query.py` and `test_sql.py`.
- The client side of §4.1 lands in Phase 6, step 2. It needs the core hook.
- Deploys: phase4 (query), T2 (routes, `DeploymentV21`), T7 (alarms).
