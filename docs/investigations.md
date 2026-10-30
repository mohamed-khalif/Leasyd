# Automatic investigations

When an alert fires (or another tool sends one), Leasyd opens an **investigation** without anyone
asking, runs it in the background and delivers the outcome. Code: `services/investigator/`. Stack:
`obs-phaseI1`.

## Shape of the code

```
services/investigator/
  domain/     pure Python: Investigation, Trigger, Evidence, Result, the state machine (model.py),
              the payload parsers (triggers.py), and what happens to a trigger (workflow.py)
  agent/      the agent (slice 1: stub.py, which says "not sure")
  ports/      the interfaces the core needs: Store, Queue, Agent, Clock
  adapters/   the ports on AWS (aws.py: S3 + DynamoDB store, SQS queue); the only boto3 import
  handlers/   thin Lambda entry points: ingest.py (alerts, webhooks, app API), worker.py (the queue)
  tools/      the agent's read-only tools (slice 2)
  eval/       the eval harness (slice 2 on)
  tests/      fakes.py (in-memory ports), test_domain.py (no AWS), test_aws.py (moto)
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

## Deploy (slice 1)

```
infra/deploy-phase0.sh     # boundary: the worker reads the queue
infra/deploy-phaseI1.sh    # queue, DLQ, obs-investigator, obs-investigator-worker
infra/deploy-phaseT2.sh    # routes: the webhook and /v1/app/investigations
infra/deploy-phaseA1.sh    # firing rules start investigations (picks up obs-phaseI1 by itself)
infra/deploy-phaseT5.sh    # tenant deletion purges investigations
infra/deploy-phaseW1.sh    # the rule form's Investigate switch
```

## Verify

1. A webhook: make the URL (above), `curl` it with `{"title": "test", "service": "checkout"}` → `202`
   with an id; `GET /v1/app/investigations/<id>` within seconds shows `completed`, root cause
   `not sure` (the stub), events `triggered, running, completed`.
2. The same `curl` again → `"joined": true`, same id.
3. An alert: a check rule firing creates one (`obs-investigator` log `{"investigation": ...,
   "source": "alert"}`); with the rule's Investigate switch off, none.
4. Tests: `cd services/investigator && python -m pytest tests` (18), `services/alerts` (28).

## Assumptions and left out (slice 1)

- The agent is a stub; delivery is a log line (`investigation_delivery_pending`). Slice 2 and 4.
- The skipped (rate limit) notice is recorded and queued for delivery; it reaches people in slice 4.
- No investigations page in the app yet (API only); the chatbot link is slice 6.
- Investigations are kept until the tenant is deleted (no 30-day expiry): they are the memory of slice 5.
- The investigation quota is the hourly cap (20 per tenant), separate from the chat's daily limit.
  A daily or monthly quota per plan can sit beside it once pricing is decided.
- SNS messages aren't signature-checked: the secret webhook token is the authentication, and the only
  URL ever fetched is an `https://sns.<region>.amazonaws.com` confirmation URL.
- Up to 10 investigations run at once across all tenants (the queue trigger's maximum concurrency).
