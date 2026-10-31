# Automatic investigations

When an alert fires (or another tool sends one), Leasyd opens an **investigation** without anyone
asking, runs it in the background and delivers the outcome. Code: `services/investigator/`. Stack:
`obs-phaseI1`.

## Shape of the code

```
services/investigator/
  domain/     pure Python: Investigation, Trigger, Evidence, Result, the state machine (model.py),
              the payload parsers (triggers.py), and what happens to a trigger (workflow.py)
  agent/      claude_agent.py (the loop, limits, checks on the finding), prompts.py (the versioned prompt),
              redact.py (secrets and personal data out), stub.py (says "not sure", no model)
  ports/      the interfaces the core needs: Store, Queue, Agent, Clock, LLMClient, Telemetry, Prompts
  adapters/   aws.py (S3 + DynamoDB store, SQS queue), claude.py (Claude, the AI SRE's query tools, the
              prompt from SSM); the only boto3 and anthropic imports
  handlers/   thin Lambda entry points: ingest.py (alerts, webhooks, app API), worker.py (the queue)
  tools/      telemetry.py: the agent's read-only tools; each call becomes an Evidence record
  eval/       the eval harness: 12 incidents with known causes (cases.py, fixtures.py), harness.py
  tests/      fakes.py (in-memory ports), test_domain.py, test_aws.py (moto), test_agent.py
              (scripted model), test_eval.py (the harness)
```

## Flow

```
alert fires (obs-alerts, rule.investigate on) ─┐
webhook /v1/investigations/webhook/{token} ────┴─▶ obs-investigator
      dedupe: same key within 30 min joins the open investigation
      hourly cap (20/tenant): over it, recorded as "skipped" and still queued for delivery
      ─▶ SQS obs-investigations (DLQ after 3 attempts)
      ─▶ obs-investigator-worker: queued → running → completed (or failed on the 3rd attempt)
      ─▶ queues {"deliver": ...}  (slice 4; logged until then)
```

## The agent (slice 2)

Claude, with four read-only tools over the tenant's telemetry (through obs-query, as the AI SRE):

| Tool | What |
|---|---|
| `list_services` | every service: requests/s, error %, p95 |
| `query_metrics` | PromQL over metrics, spans and logs |
| `query_logs` | log search, or top groups (most frequent errors) |
| `query_traces` | span search, top groups, or one whole trace |
| `get_recent_changes` | recorded deploys and versions first seen in the telemetry; by default the day before the window |
| `get_service_context` | one service: versions (requests, errors each), runtime, what it calls and what calls it, last week's deploys |

- **Tenant and window are set in code**, not by the model: the window is an hour before the alert
  started to 20 minutes after (or now); a query may move at most a day outside it.
- **Every tool call is evidence** `ev1, ev2, …`: the query, the window, a summary and a link to the
  same data in the app. The model gets results wrapped in `<evidence id="evN">…</evidence>`, told it's
  data, not instructions.
- **Redaction** before anything reaches the model (tool results and the alert): keys, tokens,
  passwords, JWTs, private keys, emails, card numbers, phone numbers. IPs, ids and order numbers stay.
  Counts are logged per call (`tool_calls[].redacted`).
- **The finding** (`submit_investigation`) is checked in code: every cited id must be one the tools
  returned; a cause with no evidence is refused; "not sure" is capped at confidence 0.3;
  `safe_to_auto_act` is always false. A refused finding goes back with the reason (twice), then the
  result is "not sure" with the evidence gathered.
- **Limits**: 16 model calls, 400k tokens, 5 minutes. On a limit, the next call must submit; then
  "not sure".
- **Logged on the investigation**: `tool_calls` (name, input, ms, evidence id or error), `cost`
  (tokens, calls, USD, why it stopped), `versions` (model, prompt, tools).
- **Configurable**: `ClaudeModel`, `PromptParam` (an SSM parameter under `/obs/investigator/` with
  `{"version", "system"}`), prices. An Anthropic API key at `/obs/anthropic/api_key` switches from
  Bedrock to the Claude API (as the AI SRE does).

## Deploys (slice 3)

Investigations line changes up with the incident. Two sources:

- **Recorded**: CI posts each deploy to `POST /v1/deploys` with the team's ingest API key (the key
  SDKs already use; it can send, not read). Kept 90 days.
- **Inferred**: a service's `service.version` (an OpenTelemetry resource attribute) first seen at a
  time, from the per-minute service summaries (`svc_1m`). Works with no CI change; catches deploys
  CI didn't report. A version that was already running at the start of the range isn't a change.

```
POST https://ingest.leasyd.com/v1/deploys
x-api-key: <ingest key>
{"service": "payment", "version": "3.1.0",                                   required
 "environment": "production", "commit": "9f2c1e7", "repository": "shop/payment",
 "url": "https://github.com/...", "description": "currency refactor",
 "status": "succeeded | started | failed | rolled_back", "deployed_at": "2026-10-30T02:11:30Z",   default: now
 "id": "gh-811"}                                                               default: from service, version, environment, time
-> 201 {"deploy": "<id>", "deployed_at": "..."}      the same deploy posted twice is one deploy
```

`GET /v1/app/deploys?service=&days=7` (signed in) lists them, newest first.

GitHub Actions, as the last step of the deploy job (secret `LEASYD_API_KEY`: an ingest key):

```yaml
      - name: Record the deploy in Leasyd
        if: always()
        env:
          LEASYD_API_KEY: ${{ secrets.LEASYD_API_KEY }}
          SERVICE: payment                          # the service.name your telemetry uses
          STATUS: ${{ job.status == 'success' && 'succeeded' || 'failed' }}
        run: |
          curl -fsS -X POST https://ingest.leasyd.com/v1/deploys \
            -H "x-api-key: $LEASYD_API_KEY" -H 'Content-Type: application/json' \
            -d "{\"service\": \"$SERVICE\", \"version\": \"${GITHUB_REF_NAME}-${GITHUB_SHA::7}\", \"environment\": \"production\",
                 \"commit\": \"$GITHUB_SHA\", \"repository\": \"$GITHUB_REPOSITORY\", \"status\": \"$STATUS\",
                 \"url\": \"$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID\", \"id\": \"gh-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT\"}" \
            || echo "Leasyd: deploy not recorded"     # never fails the deploy
```

Use the same version string your app sets as `service.version`, and the recorded and inferred
changes match up.

## Eval

`services/investigator/eval/`: 12 incidents with known causes: a dependency timing out, a connection
pool, a bad release, out of memory, a 429 rate limit, a full disk, an expired certificate; and four
where nothing changed or nothing can be known (DNS outside the services, a traffic spike, a blip on
3 requests, no telemetry at all), plus one whose logs try to steer the agent.

Scores: **accuracy** (names the cause, or honestly "not sure", and none of the wrong turns),
**evidence hit** (cites the evidence that shows the cause), **evidence precision** (cited evidence
held data), **false-confidence rate** (wrong at confidence ≥ 0.5).

```
cd services/investigator
python -m eval.harness --replay                        # the recorded run (no model calls)
CLAUDE_MODEL=anthropic.claude-opus-5-5 ANTHROPIC_KEY_PARAM=/obs/anthropic/api_key \
  python -m eval.harness --live [--record] [--only dns_elsewhere]     # about $0.20 a case
```

First live run (prompt `inv-2026-10-31.1`, recorded in `eval/recordings/`): accuracy 12/12,
evidence hit 100%, evidence precision 93%, false confidence 0%, $2.07 for all 12 (about $0.17 each).
On the traffic spike it said "not sure" at 0.3 with load as its best guess; on the blip, "no system
failure, one declined card".

Replay plays a saved run back; it checks the code around the model, not a new prompt (a changed
prompt needs `--live`).

Statuses: `queued → running → completed | failed`, or `queued → skipped`. Every change is also an
entry in the investigation's event log. A message delivered twice does nothing the second time.

Storage: `s3://obs-data-<account>-<region>/_investigations/tenant=<T>/<id>.json` (the whole record),
plus rows in `obs-tenants`: `inv#<T>#<id>` (index, version for optimistic locking),
`invdedupe#…`, `rate#inv#<T>#<hour>`, `invhook#<sha256(token)>`. All carry `tenant`, and the S3
prefix is purged when a tenant is deleted.

## Webhook

An owner gets the tenant's URL (shown once; a new one retires the old):

```
POST /v1/app/investigations/webhook-token   (signed in)  ->  {"url": "https://ingest.leasyd.com/v1/investigations/webhook/<token>"}
```

The URL accepts:

| Sender | Body | Starts an investigation when |
|---|---|---|
| CloudWatch alarm via SNS | SNS `Notification` with the alarm JSON | `NewStateValue` is `ALARM` |
| SNS subscription | `SubscriptionConfirmation` | confirmed automatically (only an `https://sns.<region>.amazonaws.com` URL) |
| PagerDuty (webhooks v3) | `{"event": {"event_type": ...}}` | `incident.triggered` |
| Anything else | `{"title", "service"?, "severity"?, "dedupe_key"?, "started_at"?, "url"?, "details"?}` | always |

Answers: `202 {"investigation", "joined", "status"}`, `202 {"ignored": why}`, `200 {"confirmed": true}`,
`400` for an unreadable body, `404` for an unknown token.

```
curl -X POST "$URL" -H 'Content-Type: application/json' -d '{"title": "Queue backing up", "service": "orders"}'
```

## App API

```
GET /v1/app/investigations          the tenant's investigations, newest first
GET /v1/app/investigations/{id}     one in full: trigger, events, result
```

Alert rules have `investigate` (default `true`); the rule form has an "Investigate" switch.

## Deploy

```
infra/deploy-phase0.sh     # boundary: the worker reads the queue
infra/deploy-phaseI1.sh    # queue, DLQ, obs-investigator, obs-investigator-worker
infra/deploy-phaseT2.sh    # routes: the webhook and /v1/app/investigations
infra/deploy-phaseA1.sh    # firing rules start investigations (picks up obs-phaseI1 by itself)
infra/deploy-phaseT5.sh    # tenant deletion purges investigations
infra/deploy-phaseW1.sh    # the rule form's Investigate switch
```

Slice 3: `infra/deploy-phaseI1.sh` (the new tools), then `infra/deploy-phaseT2.sh` (the deploy routes
and the key authorizer allowing `POST /v1/deploys`).

Slice 2 was only `infra/deploy-phaseI1.sh` again (it now installs the Anthropic SDK; `Agent=claude` is
the default; `--parameter-overrides Agent=stub` switches the model off).

## Verify

1. A webhook: make the URL (above), `curl` it with `{"title": "test", "service": "checkout"}` → `202`
   with an id; `GET /v1/app/investigations/<id>` within a minute or two shows `completed` with a
   result (root cause, confidence, evidence links), `tool_calls`, `cost` and `versions`.
2. The same `curl` again → `"joined": true`, same id.
3. An alert: a check rule firing creates one (`obs-investigator` log `{"investigation": ...,
   "source": "alert"}`); with the rule's Investigate switch off, none.
4. Tests: `cd services/investigator && python -m pytest tests` (31), `services/alerts` (28).
5. Eval: `python -m eval.harness --replay`.

## Assumptions and left out

- Delivery is a log line (`investigation_delivery_pending`) until slice 4.
- No past incidents yet (slice 5).
- Deploys: no page in the app (API only), no GitHub app (the Actions step above). Inferred versions
  are per minute and one per minute: during a rolling deploy the summary shows one version a minute.
  Recorded deploys aren't checked against the telemetry's versions.
- Cost uses configured prices (default $5 / $25 per million input / output tokens); no prompt
  caching or extended thinking yet.
- The eval's telemetry is synthetic (small pools, weighted counts), not recorded from real incidents.
- Investigations don't use up the tenant's daily search allowance (they have the hourly cap).
- The skipped (rate limit) notice is recorded and queued for delivery; it reaches people in slice 4.
- No investigations page in the app yet (API only); the chatbot link is slice 6.
- Investigations are kept until the tenant is deleted (no 30-day expiry): they are the memory of slice 5.
- The investigation quota is the hourly cap (20 per tenant), separate from the chat's daily limit.
  A daily or monthly quota per plan can sit beside it once pricing is decided.
- SNS messages aren't signature-checked: the secret webhook token is the authentication, and the only
  URL ever fetched is an `https://sns.<region>.amazonaws.com` confirmation URL.
- Up to 10 investigations run at once across all tenants (the queue trigger's maximum concurrency).
