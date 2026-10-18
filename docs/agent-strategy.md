# Leasyd as an agent that delivers outcomes: strategy

Status: **Phase 1 done (2026-10-18). Phases 2–6 not started; waiting for review of Phase 1.**
No code was changed for this document.

Method:
- Three read-only audits ran in parallel, covering the backend, the web app, and the AI/MCP layer.
- Their key claims were then re-checked against the code.
- Every claim cites a file. Paths are relative to the repository root.
- Assumptions are marked **Assumption**.
- A one-page summary for non-technical readers (`docs/agent-summary.md`) comes with Phase 6.

## Phase 1: ground truth

### 1.0 Corrections to the brief

| Claim in the brief | What the code says |
|---|---|
| "Parts of an AI layer already exist (AI insights…)" | **Partly true.** The "Live insights" on the AI page use no AI. They are seven PromQL queries plus fixed thresholds, computed in the browser (`services/web/src/pages/Ai.tsx:2-3, 99-187`). There is no server-side insights or anomaly service, and no scheduled AI run. The AI SRE (`services/ai/sre.py`) only runs when a person asks it a question. |
| "MCP access" | **True.** `POST /v1/mcp` with a `read` key, nine tools, all read-only (`services/ai/mcp.py:53-59`; verified on AWS 2026-10-05, `docs/STATUS.md:63`). |
| "API keys" | **True, but coarse.** There are three fixed scopes: `ingest`, `read` and `browser` (`services/ingest/authorizer.py:14-20, 47`). There is **no write scope**: no API key can change anything. |
| "A query endpoint with job/cancel lifecycle" | **True.** A query becomes a job after 20 s. `DELETE /v1/query/{job}` (and `/v1/app/query/{job}`) writes an S3 cancel marker, which workers poll every 5 s (`services/compaction/query.py:448, 1015-1068`). |
| "Per-tenant limits" | **True.** Per tenant: queries per minute, 25 queries in flight, daily search units (`query.py:1112-1118`); AI turns per day (`services/ai/sre.py:47`); MCP calls per minute and per day (`mcp.py:37-38`); a daily data cap on free accounts; and object counts (rules, checks, keys…). |
| "Dynamic scope labels" | **Not found.** Nothing in the code is called that. The nearest things: the fixed key scopes above; OTel instrumentation scope, which is just a `scope_name` column (`query.py:83-95`); `checks: ["*"]`, which covers future checks in maintenance windows and alert rules (`services/synthetics/reliability.py:51-54`); dashboard variables such as `$service_name` (`services/alerts/dashboards.py:4-6`); and Kubernetes pod labels as resource attributes. **Question for you:** what did you mean by it? |
| Earlier bugs where "failures showed as all clear" | **Fixed in the web app for the cases found** (`services/web/src/failures.test.tsx`). Phase 1 found **more cases, in both the web app and the alert evaluator** (§1.4). They matter more for an agent, because an agent would rely on exactly these signals. |

Also stale: `docs/ARCHITECTURE.md` §8.2–8.3 still say "Cancel: none", "per-tenant concurrency: none", and that the query runs twice after 20 s. All three were fixed in Phase 4/B14 (`query.py:1015-1068, 1260-1323`).

### 1.1 What exists that an agent could build on

Columns:
- **Can act** = can it change anything.
- **Trust** = how far its output can be relied on today.

**Telemetry**

| Capability | What it does | Data it reads | Can act | Trust today |
|---|---|---|---|---|
| **Query engine** (`services/compaction/query.py`) | JSON, PromQL and read-only SQL over logs, spans and metrics; 30-day retention | S3 Parquet per tenant | No | High for what it returns. Results are capped at 10,000 rows / 5.5 MB, and the answer then says `truncated`. Some pages ignore that flag (`docs/code-audit.md` B13). |

**AI**

| Capability | What it does | Data it reads | Can act | Trust today |
|---|---|---|---|---|
| **AI SRE** (`services/ai/sre.py`, `obs-ai`) | Answers a person's question by calling 9 read-only tools; up to 25 tool rounds and 600 s | Everything, through the query engine (billed against search units); checks and alert rules from DynamoDB | **No.** "has no tool that changes anything" (`sre.py:12-13`; prompt line `sre.py:129-130`) | Unmeasured. There are no evals, only a scripted fake-model test of the mechanics (`services/ai/test_sre.py:35-51`) and one manual planted-fault test (`STATUS.md:55`). The only evidence it links is a trace id. Nothing checks that a cited id exists. |
| **MCP server** (`services/ai/mcp.py`) | The same 9 tools for outside agents (Claude Code, Cursor…) | Same | No (`readOnlyHint`, `mcp.py:58`) | Same as the AI SRE. After its 24 s limit, a timed-out tool keeps running and is still billed (`mcp.py:130-132`). |
| **"Live insights"** (`Ai.tsx:99-187`) | Fixed thresholds in the browser: error rate ≥1% and 2× the previous period; p95 ≥1.5×; traffic −50%; check uptime <100% | 7 PromQL queries + check list | No | Medium. Failures now show as errors (`failures.test.tsx:35-45`). It is TypeScript in the browser, so a server agent can't reuse it without porting. |

**Alerting and uptime**

| Capability | What it does | Data it reads | Can act | Trust today |
|---|---|---|---|---|
| **Alert rules** (`services/alerts/alerts.py`) | Three rule types: `query` (PromQL, every 1/5/15 min), `slo_burn` and `check_failing`; evaluated every minute by `obs-alerts` (`infra/phaseA1-alerts.yaml:113-127`) | `alert#`, `astate#` items; the query engine | It **notifies** (email, Slack, signed webhook; `alerts.py:91-113`). No PagerDuty. | **Low in edge cases.** No data counts as OK and resolves alerts; failed evaluations are silent (§1.4). |
| **Synthetic checks** (`services/synthetics/synthetics.py`) | HTTP (1–10 steps) and browser checks, every 1–60 min; run results are written as the tenant's telemetry | The target URLs | Runs checks; can be run on demand | High. Secrets are masked at record time (`synthetics.py:435, 449-458`). |
| **Maintenance windows, run exclusions** (`reliability.py`) | Runs inside a window are marked excluded; single runs can be excluded with a reason, a `by` and a time | — | Through the API | High. Exclusions are reversible and attributed (`synthetics.py:974-986`). |
| **SLOs** (`reliability.py:137-159`) | Definitions stored on the server. **They cover synthetic-check results only** (no trace or metric SLOs). | Check runs | — | Medium. Attainment for display is computed in the browser (`Slos.tsx:36-46`). The server computes it only inside burn alerts (`alerts.py:418-446`). There is no server endpoint for SLO status. |

**Configuration and account**

| Capability | What it does | Data it reads | Can act | Trust today |
|---|---|---|---|---|
| **Dashboards** (`services/alerts/dashboards.py`) | Saved PromQL panels with variables | — | Through the API | High. The **only** resource with versions (409 on a stale write) and `updated_by` (`dashboards.py:146-159`). |
| **Drop rules** (`services/ingest/droprules.py`; `services/tenants/account.py:328-385`) | Discard matching data as it arrives (or keep a percentage); applied within 30 s (`services/ingest/ingest.py:55`) | — | Through the API, **owners only** | High mechanically. **Dropped data is gone for good** (`droprules.py:2`). The only preview is in the browser; there is no server preview. |
| **Usage, meters, billing** (`account.py:388-400`; `services/tenants/billing.py`) | Records received and dropped per signal per day (31 days); Stripe meters reported daily | Meter items | No | Counts are exact. **Cost is computed in the browser** from `services/web/src/pricing.ts`. A failed signal currently counts as $0 (§1.4). |
| **Kubernetes and GPU views** (`services/web/src/infra/`) | Problems, recommendations and cost from OTel collector data | SQL in `infra/queries.ts`, contract-tested against the engine (`services/compaction/test_infra_queries.py`) | No | The SQL is reusable as-is. The logic (problems, cost, recommendations: `infra/analysis.ts`, `Kubernetes.tsx:83-151`) is browser-only. Some panels show "all fine" when one of their queries fails (§1.4). |

### 1.2 Actions the product can take for a customer

**Every action that changes something needs a signed-in person** (a Cognito token on `/v1/app/*`). The tenant comes from the token. API keys reach only data ingest, queries and MCP (`authorizer.py:47`). So **today an agent can act only by impersonating a user's session**, and there is no safe way to give it that.

Roles: owner, member, viewer (`services/tenants/admin.py:543`).
- **Members can create, change and delete** alerts, checks, SLOs, windows and dashboards (`alerts.py:644-660`, `synthetics.py:137-140`).
- **Only owners** can change people, keys, drop rules, GPU prices and billing (`account.py:212-215`).
- There are no finer permissions.

**Monitoring setup**

| Action | API | Who | Reversible? | Dry run? |
|---|---|---|---|---|
| Create / change / delete an alert rule | `POST`, `PUT`, `DELETE /v1/app/alerts/rules[/{id}]` | member+ | `PUT` overwrites blindly; `DELETE` is hard; `enabled:false` disables | **Yes:** `POST …/rules/preview`, and PromQL is run once on save (`alerts.py:737-752`) |
| Notification channels | `/v1/app/alerts/channels[/{id}[/test]]` | member+ | Delete refused while a rule uses it | Test message |
| Synthetic check CRUD; pause | `/v1/app/checks[/{id}]` | member+ | Overwrite / hard delete / `enabled:false` | **Yes:** `POST /v1/app/checks/test` |
| Run a check now | `POST /v1/app/checks/{id}/run` | member+ (10 per minute) | — | — |
| Exclude / re-include a run | `POST`, `DELETE …/exclusions[/{run}]` | member+ | **Yes**, attributed | — |
| Maintenance window CRUD | `/v1/app/windows[/{id}]` | member+ | Hard delete | — |
| SLO CRUD | `/v1/app/slos[/{id}]` | member+ | Overwrite / hard delete | — |
| Dashboard CRUD | `/v1/app/dashboards[/{id}]` | member+ | **Versioned** (409 on a stale write) | — |

**Account, data and AI**

| Action | API | Who | Reversible? | Dry run? |
|---|---|---|---|---|
| Replace all drop rules | `PUT /v1/app/account/drop-rules` | owner | The rule can be reverted; **the data it dropped can't be recovered** | Browser only |
| GPU prices | `PUT /v1/app/account/gpu-prices` | owner | Overwrite | — |
| Invite / remove a user; create / revoke a key | `/v1/app/account/users`, `…/keys` | owner | Removal is hard | — |
| Run / cancel a query | `/v1/app/query`, `/v1/query` | user or `read` key | — | — |
| Ask the AI | `/v1/app/ai/conversations` | user | — | — |

**Exists only as a click, or only for operators:**
- **Browser-only state:**
  - Dashboard favourites, columns, folds, SQL text and theme are kept in the browser (localStorage).
  - Query Builder state lives in the link and is saved only when added to a dashboard (`services/web/src/pages/QueryBuilder.tsx:84-91`).
- **Operator-only actions:** plan changes, caps, trial extension, tenant deletion and restore are in `admin.py` (`ACTIONS`, `admin.py:920-925`) through `infra/tenant.sh`, with no customer API.
- **Not found anywhere:**
  - alert acknowledgement;
  - snooze or mute with an expiry (only `enabled:false`);
  - changing a user's role;
  - a drop-rule preview on the server;
  - incidents;
  - deploy or change events;
  - any ticketing or PagerDuty integration.

### 1.3 Gaps that block an agent

**Ability to act**
1. **No way to authorise an agent to act.** There is no write scope for API keys and no service identity, so all writes need a person's Cognito session (§1.2).
2. **Permissions are by role, not by action.** "May tune alert thresholds but not delete checks" can't be expressed. Members can delete any alert or check.

**Record-keeping and safety**

3. **No audit log.**
   - What exists is uneven: `created_by` on most items; `updated_by` only on dashboards, drop rules and GPU prices; `by` on exclusions; and a few log lines (`key_created`, `drop_rules_saved`, `account.py:220, 254, 260`).
   - `PUT` on alerts, checks, SLOs and windows loses who edited (`alerts.py:753`, `synthetics.py:990, 1052`).
   - Nothing records what a change replaced, so nothing can be undone automatically.
4. **No versions except dashboards.**
   - Alerts, checks, SLOs and windows are written as blind overwrites, so an agent and a person can silently overwrite each other's changes.
   - Deletes are hard.
5. **No way to represent "proposed".** There is no proposal object, no approval step, and no "the agent did this" marker.

**Information the agent would need**

6. **No feedback signal.**
   - There are no thumbs up/down on AI answers.
   - There is no alert acknowledgement or "this was noise" marking.
   - There are no incident records or resolution times.
   - So MTTA/MTTR and noise can't be measured today (§1.5).
7. **No memory across conversations.** Each conversation stands alone (`sre.py:519-545`). There are no learned facts and no runbooks.
8. **No topology or change data.**
   - The service map is computed in the browser from spans (`services/web/src/pages/ServiceMap.tsx:1-5`).
   - There are no deploy events, only the `service.version` attribute when a customer sends it.
9. **Key analysis logic exists only in the browser.**
   - Insight thresholds, Kubernetes problems, GPU cost and recommendations, the usage-cost estimate and the billing estimate are all browser-only.
   - SLO numbers are the only part already in Python (`alerts.py:418-450`).

**Running safely**

10. **Nothing runs proactively.** The AI runs only when asked; there is no schedule or event trigger for it (`infra/phaseAI-sre.yaml:52-57, 108-114`).
11. **No defence against prompt injection beyond one sentence in the prompt** (`sre.py:122-123`).
    - Log bodies, span attributes, RUM error messages from customers' end users (a public route, `services/ingest/rum.py`), and check failure text all reach the model as plain JSON.
    - There is no redaction of secrets or personal data in the AI path. Only synthetics masks at record time.
    - A crafted `#/ai?ask=…&page=…` link starts a question as the signed-in user (`Ai.tsx:24-34`). That costs one turn but does nothing else, because the AI only reads.
12. **Cost controls are counts, not money.**
    - The AI is limited to 20/200 turns per day. Its tool queries use search units.
    - Tokens per call are logged (`sre.py:615-623`) but not stored or budgeted. The measured cost is about $0.09–0.11 per question (`STATUS.md:55`).
    - The AI's daily counters never expire (no `expires`, `sre.py:552-562`, unlike MCP `mcp.py:157`).
    - The AI's IAM allows `PutItem`/`UpdateItem` on the whole `obs-tenants` table (`infra/phaseAI-sre.yaml:46-51`), broader than its code needs.

### 1.4 Trust: where a failure can still look like "all clear"

An agent that reads these signals would inherit these failures. All were found in Phase 1 and are confirmed in the code.

**Backend: alert evaluation (`services/alerts/alerts.py`)**
- **No data counts as OK.**
  - A NaN value is `ok` (`:537-538`).
  - A series missing from the result is **resolved, and "Back to normal" is sent** (`:596-600`).
  - So if data stops arriving (the app crashed, or the collector died), firing alerts resolve.
- **A failed evaluation is silent.**
  - It is only logged as `check_rule_failed` or `slo_rule_failed` (`:494-498, 555-560`).
  - State is kept, and nothing tells the customer.
- **A check that stopped running** (disabled, or the trial ended) leaves its `check_failing` alert in its last state indefinitely, because no result arrives (`synthetics.py:829-835, 1089-1098`).
- **An SLO with no runs** resolves with "Now: no runs yet" (`:443-446, 499-514`).

**Web app**

| Where | What happens on failure | Evidence |
|---|---|---|
| Home, checks Healthy/Degraded | Uptime query fails → `?? 1`, so every check counts as healthy | `services/web/src/pages/Insights.tsx:66` |
| SLOs, "at risk" | The 1-hour query fails → burn treated as 0, so it can read "healthy" | `pages/Slos.tsx:42-43, 70-73` |
| Usage & cost | A failed signal counts as $0, so the total is understated | `pages/settings/UsageCost.tsx:50-54` |
| Kubernetes "Needs attention" | Gated only on the pods query; nodes/workloads failing can still read "all ready" | `pages/Kubernetes.tsx:128, 150` |
| GPU workloads and health | "Nothing to reclaim", $0 spend and 0 counts when a query fails | `pages/Gpus.tsx:199-213, 290-295` |
| Drop rules, AI conversation list | A meters or list failure is shown as "nothing yet" | `pages/DropRules.tsx:59, 110`; `Ai.tsx:21` |

These are bugs today, independent of any agent. A separate fix is recommended (behaviour change: shows errors instead of "fine"; your call).

### 1.5 What can be measured today (needed for Phase 2)

| Measure | Possible today? |
|---|---|
| Telemetry volume, drops and cost per signal per day | **Yes.** Exact meters, 31 days (`account.py:388-400`). Per service: by query (24 h). |
| Synthetic uptime and SLO attainment | **Yes.** Checks only. |
| Alert firings and resolutions | **Partly.** Only as tenant log records (service `alerts`), which expire with the 30-day retention (`alerts.py:352-368`). |
| Alert noise (a firing nobody needed) | **No.** There is no acknowledgement or "noise" mark. |
| MTTA, MTTR | **No.** There are no incidents or acknowledgements. Firing-to-resolved time is only a proxy, and it is inflated by the "no data resolves" bug. |
| Root-cause accuracy | **No.** There are no evals and no feedback. |
| AI cost per answer | **Logs only** (`ai_usage` tokens, `sre.py:615-623`). |

## Phase 2: outcomes

*Not started. Waiting for review of Phase 1.*

## Phase 3: agent architecture

*Not started.*

## Phase 4: trust, autonomy and safety

*Not started.*

## Phase 5: product and business

*Not started.*

## Phase 6: roadmap and decisions

*Not started.*
