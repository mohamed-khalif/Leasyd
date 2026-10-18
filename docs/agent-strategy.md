# Leasyd as an agent that delivers outcomes: strategy

Status: **Phases 1–2 done (2026-10-18). Phases 3–6 not written, by decision; their short form is `docs/agent-summary.md`.**
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
| "Dynamic scope labels" | **Planned, not built** (corrected by the founder, 2026-10-18: the brief listed it as existing by mistake). Nothing in the code is called that today. The nearest things: the fixed key scopes above; OTel instrumentation scope, which is just a `scope_name` column (`query.py:83-95`); `checks: ["*"]` for future checks (`services/synthetics/reliability.py:51-54`); dashboard variables (`services/alerts/dashboards.py:4-6`); and Kubernetes pod labels as resource attributes. **Treated in this document as a dependency for the agent**, not an existing feature. **Assumption** about what it will be: labels (service, environment, team…) that scope what a key, a user or the agent may see and change. |
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

## Phase 2: outcomes, not features

**Baselines.** Leasyd has no paying customers' history yet. So every "baseline" below is **measured per customer during the first two weeks**, read-only. Before that, no claim of improvement can be made.

**Prerequisite for all of them: the all-clear fixes** (§1.4, agreed 2026-10-18). An agent that reads "resolved" when data stopped would report wrong outcomes.

### 2.1 Candidate outcomes

#### O1. Incidents are triaged before a human looks

- **Customer outcome:** when an alert fires, the person paged already has a short explanation:
  - what changed;
  - where (service, endpoint, trace ids);
  - since when;
  - how sure the agent is, and what to look at next.
- **How we measure it:**
  - % of firings with a summary within 3 minutes;
  - time from firing to summary;
  - human rating of each summary: right, partly right, wrong (**needs a feedback button: gap 6**);
  - accuracy on a replay set of past and planted faults.
- **Baseline today:** 0% (the AI only answers when asked).
- **What "good" looks like:**
  - more than 90% of firings get a summary within 3 minutes;
  - "right or partly right" at least 70% on the replay set before launch, and in production samples.
- **What the agent does:**
  - **Detect:** the alert fires; we already detect this.
  - **Investigate:** the existing AI SRE tools.
  - **Decide:** rank hypotheses.
  - **Act:** none in customer systems; post to the alert's channels and the firing record.
  - **Verify:** check its cited evidence exists.
  - **Report:** the summary itself.
- **Data and permissions:** read-only on telemetry, checks and alerts, plus "post a message to the rule's channels". No write access anywhere.
- **Worst case:**
  - a confident wrong summary sends the on-call engineer the wrong way and costs time;
  - text planted in logs steers the summary (§1.3 gap 11);
  - the cost of long investigations during an alert storm.

#### O2. Telemetry spend goes down without losing data anyone uses

- **Customer outcome:** "We pay for the data we use."
- **How we measure it:**
  - stored and ingested records and $ per service per month (exact meters, `account.py:388-400`);
  - plus a guard: no dashboard, alert or recent query reads data the agent proposed to drop.
- **Baseline today:** measured per customer. **Gap:** there is no record of what data is queried (`services/compaction/query.py` logs no query shape), so "unused" can't be computed yet.
- **What "good" looks like:** 20–40% less stored volume (**Assumption**, based on debug logs and health-check spans typically being a large share). There must be zero cases of "the agent dropped what an incident needed".
- **What the agent does:**
  - **Detect:** high-volume, never-read streams.
  - **Investigate:** samples and owners.
  - **Propose:** a drop or sampling rule with its expected saving.
  - **Approve:** a person approves it.
  - **Verify:** with the meters over the next days.
  - **Report:** "saved $X".
- **Data and permissions:**
  - meters;
  - query history (new);
  - the drop-rule API, which is owner-only. A scoped agent permission is needed (the planned scope labels).
- **Worst case:** **dropped data is gone for good** (`droprules.py:2`). The agent should prefer sampling (`keep_percent`) over dropping and never act without approval.
- **Business tension:** Leasyd bills on ingest and storage, so this outcome lowers revenue per customer (see Phase 5 in the summary).

#### O3. Alerts mean something: less noise, no blind spots

- **Customer outcome:** fewer pages that needed no action, and no important service without an alert.
- **How we measure it:**
  - firings per week;
  - flapping, meaning fired and resolved within 10 minutes. This can be computed from the alert history logs (`alerts.py:352-368`);
  - "% of firings marked useful" (**needs acknowledgement and feedback: gap 6**);
  - coverage, meaning the share of services with traffic that have at least one alert or check.
- **Baseline today:** flap rate and coverage can be computed now; usefulness can't.
- **What "good" looks like:** at least 50% fewer flaps; coverage of the top services by traffic at least 90%.
- **What the agent does:**
  - **Detect:** noisy or missing rules.
  - **Investigate:** **backtest** a changed threshold or `for_minutes` over 30 days of data. The engine can evaluate PromQL at past times; the rule preview exists (`alerts.py` preview).
  - **Propose:** the change, with the backtest result.
  - **Approve:** a person approves.
  - **Verify:** the next 2 weeks.
- **Data and permissions:**
  - alert rules write access (member);
  - **versioning and an audit log are needed first** (gaps 3–4).
- **Worst case:** the agent tunes away a real alert and the next real incident is missed. Every change must show the past firings it would have suppressed.

#### O4. Faster recovery (MTTR) through automatic remediation

- **Customer outcome:** incidents fix themselves: rollback, restart, scale.
- **How we measure it:** MTTR. **Can't be measured today** (no incidents, no acknowledgement, no resolution records), and recovery would be hard to attribute to the agent.
- **Baseline today:** none.
- **What the agent does:** it would need write access to the customer's cloud or deploy system. **Leasyd has no such access or integration** (§1.2).
- **Worst case:** a production outage caused by the agent.
- **Verdict:** not now. Highest blast radius, lowest verifiability. A "suggested next step" inside O1 gets most of the value.

#### O5. Every user-facing endpoint is watched (reliability coverage)

- **What the agent does:** proposes synthetic checks and check-based SLOs for the busiest endpoints it sees in traces and RUM.
- **How we measure it:** coverage %, which is objective.
- **Data and permissions:** the checks API (member). Checks are reversible, but each run is metered.
- **Worst case:** extra check costs, or checks against endpoints with side effects. Proposed checks must be GET-only.
- **Verdict:** **good, low risk, but this is closer to a feature than an outcome.** A "suggested checks" list would serve most of it without an agent.

#### O6. On-call that sleeps

- **How we measure it:** notifications between 22:00 and 07:00 local, per week. This is countable from alert history; it needs the team's time zone.
- **What delivers it:** really O1 + O3 combined. It is a good **headline** for customers, but not a separate thing to build.

#### O7. GPU and Kubernetes spend

- **What already exists:** idle-GPU and right-sizing recommendations are computed in the browser (`services/web/src/infra/analysis.ts`).
- **What the agent would add:** it can only report, because it has no access to the cluster.
- **How we measure it:** idle GPU-hours × price, which is objective.
- **Verdict:** valuable for GPU-heavy customers, a niche today. A **weekly report is a conventional feature**; it needs no agent.

### 2.2 Ranking

Scores from 1 (poor) to 5 (best). For "risk if wrong", 5 = safest.

| Outcome | Customer value | Feasible with data we hold | Risk if wrong (5 = safest) | Verifiable | Total |
|---|---|---|---|---|---|
| **O1 Triage before a human looks** | 5 | 4 (AI SRE + alerts exist; needs a trigger and feedback) | 4 (read-only; a wrong summary costs time) | 3 (replay set + ratings) | **16** |
| **O3 Alert noise and coverage** | 4 | 3 (backtest possible; usefulness needs feedback) | 3 (could tune away a real alert; reversible with versioning) | 4 (flaps and coverage are objective) | **14** |
| O2 Telemetry spend | 4 | 2 (needs query history; owner-only API) | 2 (dropped data is gone) | 5 (exact meters) | 13 |
| O5 Endpoint coverage | 3 | 4 | 4 | 4 | 15, but feature-like |
| O6 On-call that sleeps | 5 | via O1 + O3 | via O1 + O3 | 4 | headline, not a build |
| O7 GPU / K8s spend | 3 (niche) | 3 | 5 (report only) | 4 | 15, but a report |
| O4 Auto-remediation | 5 | 1 (no access) | 1 | 1 | 8 |

### 2.3 Recommendation: O1 first, then O3

**O1 first: incident triage.**
- **It is useful on day one** and needs no permission to change anything. That matches where Leasyd is: a read-only AI that already exists, with no audit log, versioning or write scope.
- **It earns the trust that every later step needs.** Customers see the agent being right, with evidence, many times before it is allowed to change anything.
- **What it needs** (small to medium, no new access):
  - an alert-fired trigger for the AI;
  - a "right / partly / wrong" button;
  - citation checks;
  - a replay evaluation set;
  - a per-tenant budget in tokens and $.
- **Its result can be checked:**
  - every summary is rated or sampled;
  - the replay set gives a number before launch.

**O3 second: alert quality.**
- Changes are **reversible**, once rules have versions and an audit log, which O3 forces us to build.
- They can be **backtested** on the customer's own 30 days of data before anyone approves.
- They are **measurable**: flaps and coverage, and usefulness once O1's feedback exists.
- It is the natural next step: the triage agent sees every firing, so it learns which alerts are noise.

**Not first:**
- **O2 (spend):** its result is the easiest to measure, but its mistakes can't be undone, it needs query history and owner-level writes, and it reduces our own revenue. Offer it later as suggestions only.
- **O4:** high blast radius. "Suggested next step" inside O1 is the safe version.

**Where an agent is hype and a feature would do better:**
- O5, O7 and the weekly summary are better as ordinary features: lists and reports with a button.
- An agent loop adds cost and uncertainty without adding much.

**Dependencies for O1 and O3:**
- **The all-clear fixes:** agreed, and first.
- **Feedback and acknowledgement on firings.**
- **Rule versioning and an audit log** (O3).
- **The planned dynamic scope labels**, so the agent's later write access can be limited to, say, "alerts of service X in staging" (O3 and beyond).

## Phases 3–6

Not written in detail, by decision (2026-10-18). The short form, in plain words, is in `docs/agent-summary.md`:
- how it would work;
- how trust is earned;
- the downsides, including prompt injection through logs;
- pricing;
- the questions for the founder.
