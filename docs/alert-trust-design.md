# Alerts never say "all clear" without evidence: design proposal

Status: **proposal, waiting for approval (2026-10-18).** Nothing is built yet. Code references are to `services/alerts/alerts.py` unless stated.

## Problems today

| # | Today | Where |
|---|---|---|
| 1 | A query value of NaN counts as **OK** | `level_of`, `:537-538` |
| 2 | A series missing from the result is **resolved, and "Back to normal" is sent**. If data stops arriving, firing alerts resolve. | `:596-600` |
| 3 | A failed evaluation is only logged for Leasyd's operators (`check_rule_failed`, `slo_rule_failed`). **The customer is never told.** State is kept, so a broken rule can stay silent for ever. | `:494-498, 555-560` |
| 4 | An SLO with no runs **resolves** with "Now: no runs yet" | `:443-446, 499-514` |
| 5 | A "check failing" alert whose check stopped running (paused, deleted, trial ended) **stays in its last state for ever, silently** | `on_result` `:382-414`; `synthetics.py:1089-1098` |

## The core difficulty

A query that returns nothing can mean two opposite things:
- **Zero.** For example, "error rate > 5/s" returns nothing because there are no errors. Leasyd's PromQL over spans returns no series when nothing matches.
- **Silence.** For example, "requests < 1/s" returns nothing because the service died, or its collector did.

The system can't tell which one it is. **So each alert says what "nothing" means**, and the default is the safe one.

## Design

### 1. Each series is in one of four states

| State | Meaning |
|---|---|
| `ok` | Has data, within limits |
| `degraded` | Has data, past the degraded threshold |
| `critical` | Has data, past the critical threshold |
| **`no_data`** (new) | Was being watched, now has no value. We can't say whether it's fine. **It never counts as resolved.** |

A NaN value is treated as missing data (problem 1).

### 2. A new per-alert setting: "If the query returns nothing, that means…"

| Value | Label in the form | Behaviour |
|---|---|---|
| **`alert`** (default) | "Something may be wrong: tell me" | See the rules below. |
| `zero` | "Zero (e.g. no errors)" | A missing series is evaluated as the value 0. So "> 5" goes back to OK and "< 1" fires. **Safety net:** if the account's data stopped arriving altogether (§4), this falls back to `alert`. |
| `keep` | "Keep the last state (sparse data, e.g. a nightly job)" | Nothing changes until data comes back. |

Rules for `alert`:
- **A firing series that disappears** goes to `no_data`. It stays visibly firing ("No data since 03:12") and is **never resolved**.
- **A non-firing series that disappears** is forgotten quietly, as today.
- **The whole result going empty**, when the rule returned data within the last 24 h, raises a rule-level `no_data`. Example: "requests < 1/s for checkout" when checkout dies.
- **When data comes back**, the series is evaluated normally. A move from `no_data` to OK is notified as "Data is back, within limits".

**Grace period.** A `no_data` state is notified only after **3 evaluations in a row** with no data:

| Rule runs every | Time before notifying |
|---|---|
| 1 min | 3 min |
| 5 min | 15 min |
| 15 min | 45 min |

One "NO DATA" message is sent per series, not repeated.

### 3. Failed evaluations

- An evaluation fails when the query errors, times out, or anything raises.
- **A failure never changes any series' state.** Firing stays firing; OK stays OK.
- Each rule keeps a counter of failures in a row (item `astate#<tenant>#<rule>#_eval`: `failures`, `failing_since`, `notified`).
- **After 3 failures in a row:** one **"CAN'T EVALUATE"** message goes to the rule's channels. It names the error, for example "the query: unknown metric x". It is also recorded in the alert history.
- **On the next success:** one **"EVALUATING AGAIN"** message. The counter resets.
- This applies to query rules and SLO rules.
- **Check-failing rules have no evaluation to fail.** They react to each check run, so for them the "stopped" case is §5.

**Leasyd-wide outage guard** (optional, recommended):
- **Trigger:** more than 50% of all rules, across all accounts, fail in the same minute. That is a Leasyd problem, not the customer's.
- **Effect:** customer "CAN'T EVALUATE" messages wait up to 10 minutes, while Leasyd's own alarm pages us. After that they go out anyway.
- **Why:** it avoids sending hundreds of customers an alarm about our own outage within the first minute.

### 4. Safety net: "this account's data stopped arriving"

- **How it's detected:**
  - Once per evaluation pass, for each account with alert rules, read today's meter (`meter#<tenant>#<day>`, records received, written by ingest within about 1 s).
  - Keep the last count and when it last grew, in item `astate#<tenant>#_ingest`.
  - If it hasn't grown for **10 minutes**, the account's data has stopped.
- **What it does while data has stopped:**
  - No rule of that account may resolve or go to OK because of missing data, whatever its setting.
  - Missing series go to `no_data`.
  - One account-level **"NO DATA ARRIVING"** message goes to each channel used by its rules.
- **Cost:** one DynamoDB read per account per minute.

### 5. SLO and check-failing rules

**SLO burn rules**
- With no runs in the window, the state is `no_data`, not resolved (problem 4).
- The same grace period and message rules apply as above.

**Check-failing rules**
- **Check paused, deleted, or no longer running because the trial or billing ended:** a firing alert is **closed** with the reason, for example "Closed: the check was paused by ana@acme.io". It is not "resolved", and the history shows "closed".
  - These are checked every 5 minutes, alongside the SLO rules.
- **Check enabled but silent for more than 3 times its frequency**, for example because a run was lost (the run Lambda's async calls have no retries, `phaseA1-alerts.yaml:109-111`): the alert goes to `no_data` with "No runs since …".

### 6. Messages and webhooks

- **New subject prefixes:**
  - `[Leasyd] NO DATA:`
  - `[Leasyd] CAN'T EVALUATE:`
  - `[Leasyd] EVALUATING AGAIN:`
  - `[Leasyd] CLOSED:`
- **The webhook `state` field** gains four values: `no_data`, `eval_failed`, `eval_ok` and `closed`.
  - **This changes the webhook contract.** A receiver that only expects `firing` or `resolved` must ignore the others. The docs page will say so.
- Every message keeps the link and the query, as today.

### 7. API and web app

**API**
- The rule body gains `no_data` (`alert` | `zero` | `keep`). It is optional, and the default is `alert`.
- `GET /v1/app/alerts/rules` adds a `health` field per rule:
  - `{evaluating: "ok" | "failing", failing_since?, error?}`;
  - the count of `no_data` series.

**Web app (Alerts)**
- **Rules list:** a grey "No data since 03:12" badge and an amber "Can't evaluate: <error>" badge.
- **Rule form:** the "If the query returns nothing" question, with the examples above.
- **History:** the new kinds.
- **Built-in templates** (`services/web/src/infra/alertTemplates.ts`) set the right value:
  - error and latency templates: `zero`;
  - traffic, crash-loop and GPU "missing" templates: `alert`.

### 8. Existing rules: a behaviour change to approve

Existing rules have no `no_data` field, so they get the safe default `alert`. The effects:
- **Error-type rules** (">" thresholds on things that disappear when fine): when errors stop, a firing alert goes to "NO DATA" instead of "RESOLVED".
  - **Mitigation:** a one-time banner on the Alerts page lists these rules, with "treat no data as zero" one click away.

**Alternative B (not recommended):**
- **How it works:** existing rules get a default from their operator: `>` / `>=` → `zero`; `<`, `<=`, `==`, `!=` → `alert`.
- **Upside:** fewer surprises.
- **Downside:** an error rule for one service that died, while the account's other services still send data, would then **resolve**. The safety net only covers the whole account stopping.

## Build plan

Each step is one commit, with a failing test written first. The backend tests use moto, as `test_alerts.py` does.

| Commit | What it does |
|---|---|
| A1 | Failed evaluations: state never changes; counter; "CAN'T EVALUATE" after 3 failures in a row; "EVALUATING AGAIN"; for query and SLO rules |
| A2 | `no_data` state and the `no_data` setting (validation, NaN, missing series, whole-result-empty, grace period of 3, messages) |
| A3 | Safety net: the account's data stopped arriving |
| A4 | SLO with no runs → `no_data`; check-failing: closed when the check is paused or deleted, `no_data` when silent |
| A5 | Webhook states and docs (`site/docs.html#alerts`), `health` in the rules API |
| A6 | Web app: badges, the form question, history kinds, templates, the one-time banner |
| (optional) | The Leasyd-wide outage guard |

Then the six pages (P1–P6), one commit each, each showing "couldn't load" instead of "fine":
- Home checks;
- SLO "at risk";
- Usage & cost;
- Kubernetes "needs attention";
- GPU workloads and health;
- Drop rules / AI list.

**Deploy:** `infra/deploy-phaseA1.sh` (alerts), then `infra/deploy-phaseW1.sh` (web).

## Decisions needed

1. **The default for existing rules:** `alert` for all, with the banner (recommended), or alternative B?
2. **Grace period:** 3 evaluations in a row for both "no data" and "can't evaluate"? (You suggested about 3.)
3. **The Leasyd-wide outage guard:** build it now, or later?
