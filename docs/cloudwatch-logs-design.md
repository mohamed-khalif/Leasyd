# CloudWatch Logs into Leasyd: design

Status: **approved and built (2026-10-18)** with the recommended answers: a chosen list of log groups by default, Lambda START/END lines kept, ECS services named after the container. Live since 2026-10-19 and checked on AWS (L5, `docs/refactor-progress.md`); the setup stack itself is still to be run in a real account. It follows the CloudWatch metrics path (`services/ingest/cloudwatch.py`, `examples/aws/cloudwatch-metrics.yaml`) as closely as it can, so most of the plumbing already exists.

## Why

- Today Leasyd takes **CloudWatch metrics** from a customer's AWS account, but **not CloudWatch Logs**.
- On AWS, most logs are already in CloudWatch Logs: Lambda, ECS (`awslogs`), API Gateway, RDS, VPC flow logs, and any app writing to stdout on AWS-managed compute.
- Getting those logs today means installing an OpenTelemetry collector or layer per workload. One CloudFormation stack that forwards what's already in CloudWatch removes that step.

## How it works

```
CloudWatch Logs log groups
  └─ subscription filter (per group, or one account-wide policy)
       └─ the customer's Firehose (HTTP endpoint destination, gzip, 1 MB / 60 s buffer)
            └─ POST /v1/aws/cloudwatch-logs   (Leasyd API key in X-Amz-Firehose-Access-Key)
                 └─ converted to OTLP logs → drop rules → the tenant's logs stream, metered as usual
```

This is the same shape as CloudWatch metrics:
- the same key header (`authorizer.py` already accepts `x-amz-firehose-access-key`);
- the same answer format;
- the same retry and backup behaviour: on an error, Firehose retries for 300 s, then keeps the data in the customer's S3 bucket.

## What arrives

Each Firehose record's `data` is base64. Inside is a **gzipped** CloudWatch Logs subscription message:

```json
{"messageType": "DATA_MESSAGE", "owner": "123456789012",
 "logGroup": "/aws/lambda/checkout", "logStream": "2026/10/18/[$LATEST]ab12…",
 "subscriptionFilters": ["leasyd"],
 "logEvents": [{"id": "3796…", "timestamp": 1791072000123, "message": "…"}]}
```

- **`CONTROL_MESSAGE`** records are AWS checking that the destination is reachable. They are skipped.
- **Both gzipped and plain records are accepted.** We look for the gzip magic bytes, because Firehose's optional "decompress" setting delivers plain JSON. If the customer also turns on Firehose's "extract message" setting, the log group name is lost, so the setup guide says to leave it off.
- **The region isn't in the message.** The template passes it as a Firehose common attribute, which arrives in the `X-Amz-Firehose-Common-Attributes` header.
- **Size guard:** each record's decompressed size is capped, as the ingest API already caps OTLP bodies (`MAX_DECOMPRESSED_BYTES`). Too large means a 400 response, which Firehose retries and then backs up.

## How each log becomes a Leasyd log

Each `logEvent` becomes one OTLP log record.

### Service name, from the log group (and stream)

| Log group | `service.name` | Extra attributes |
|---|---|---|
| `/aws/lambda/<fn>` | `<fn>`, matching the name the OTel Lambda layer gives its traces | `faas.name` |
| `/ecs/<name>` or any group whose streams look like `prefix/container/task-id` (the `awslogs` driver) | the container name | `container.name`, `aws.ecs.task.id` |
| `/aws/rds/instance/<db>/<type>` (and `/cluster/`) | `<db>` | `db.log.type` (error, slowquery, postgresql…) |
| `API-Gateway-Execution-Logs_<id>/<stage>` | `apigateway-<id>` | `aws.apigateway.stage` |
| anything else | the log group name, without a leading `/` | |

Every record also gets:
- `aws.log.group.names`, `aws.log.stream.names`;
- `cloud.provider=aws`, `cloud.account.id` (from `owner`) and `cloud.region`.

These are the OpenTelemetry names, so drop rules and queries see them like any other attribute.

### Body, severity and trace link

**Body**
- The message text, with any trailing newline removed.

**JSON messages**
- A message that is a JSON object (structured logging) is parsed:
  - `message` / `msg` becomes the body;
  - the other top-level fields become attributes (bounded in number and size).

**Severity**, in order:
1. a JSON `level` / `severity` field;
2. otherwise a leading keyword (`ERROR`, `WARN`, `INFO`, `DEBUG`, including Lambda's `[ERROR]` tab-separated format);
3. otherwise unset.

**Trace link**
- A trace id is taken from a JSON `trace_id` / `traceId` field, or from an X-Ray id (`1-5f84c7a1-…`, converted to the 32-hex form).
- A log carrying one links to its trace in Leasyd, as RUM logs already do.

**Lambda platform lines**
- These are the `START RequestId`, `END RequestId` and `REPORT RequestId` lines.
- They are kept as INFO.
- The `REPORT` line's duration, billed duration, memory used and init duration become attributes.
- So "slowest invocations" and "cold starts" can be queried from logs.

## Setup: `examples/aws/cloudwatch-logs.yaml`

A copy of the metrics template's Firehose part:
- HTTP endpoint destination;
- gzip;
- 1 MB / 60 s buffer;
- 300 s of retries;
- an S3 bucket for failed data only;
- Firehose's own error log group.

On top of that:
- **A role** that lets CloudWatch Logs write to the Firehose.
- **Parameters:**
  - `LeasydApiKey`, `LeasydEndpoint`, as for metrics;
  - `LogGroups`: a comma-separated list of log group names to send;
  - `AllLogGroups` (`false` by default): instead of the list, one account-wide subscription filter policy (`AWS::Logs::AccountPolicy`, `SUBSCRIPTION_FILTER_POLICY`) covering every log group in the region, new ones included;
  - `FilterPattern` (empty by default, meaning everything): CloudWatch's own filter syntax. For example, `?ERROR ?WARN` sends only those lines and cuts cost before Firehose.
- **Loop protection:** the account-wide policy excludes the Firehose's own error log group.

AWS limits the template has to live with:
- **2 subscription filters per log group.** A group that already has two can't be added; the stack fails on that group with AWS's message, and the guide says so.
- **1 account-wide subscription policy per region.** If the customer already has one, `AllLogGroups` fails. They use the list instead.
- **One region per stack.** As with metrics, a customer deploys the stack once per region.

The Get started page's AWS Lambda section (which already shows the CloudWatch metrics stack) gets the CloudWatch Logs stack too, plus an "AWS logs" option for ECS and other services, with the launch-stack link and a "waiting for data" check.

## Costs (for the customer)

| Item | About |
|---|---|
| CloudWatch Logs ingestion | Already paid today, unchanged |
| Subscription filter delivery | No extra CloudWatch charge for standard log groups |
| Firehose | $0.029/GB (HTTP endpoint delivery) |
| Leasyd | Normal logs ingest and storage, as for any logs |

**Ways to cut volume:**
- `FilterPattern`, before Firehose;
- drop rules in Leasyd. These are billed for ingest only, but cut storage.

Lambda `START`/`END` lines are the obvious candidates.

## Code changes

| Where | Change |
|---|---|
| `services/ingest/cloudwatch_logs.py` (new) | `firehose_messages(body)` → subscription messages; `to_otlp(messages, region)` → OTLP logs. Pure functions, like `cloudwatch.py` |
| `services/ingest/ingest.py` | `firehose_handler` takes the signal and converter as arguments, so metrics and logs share the 403 / 429 / Firehose-answer code; then `accept(tenant, "logs", doc, drop=True)` |
| `services/ingest/authorizer.py` | Route `POST /v1/aws/cloudwatch-logs`, ingest scope |
| `infra/phaseT2-ingest.yaml` | The API Gateway resource and method, beside `cloudwatch-metrics` |
| `examples/aws/cloudwatch-logs.yaml` (new), `examples/aws/README.md` | The setup stack and its guide |
| `site/docs.html`, `services/web/src/pages/GetStarted.tsx` | Docs section and the Get started option |
| Tests | Converter unit tests (gzip and plain, control messages, each log group type, JSON and text severity, X-Ray id, REPORT lines, size guard); handler tests (auth header, 403 / 429, drop rules applied, metering); a web test for the Get started option |

## Build plan

Each step is one commit, tests first.

| Step | What | Time |
|---|---|---|
| L1 | Converter and its tests | 0.5 day |
| L2 | Route, handler, authorizer, API Gateway; handler tests | 0.5 day |
| L3 | Setup template and guide | 0.5 day |
| L4 | Docs and Get started | 0.25 day |
| L5 | Verify on AWS: a `qa-` tenant, a test Lambda and a test log group, the stack in list mode, logs visible within about 2 minutes with the right service names, severity and trace links; then delete everything | 0.5 day |
| | **Total** | **about 2.5 days** |

**Deploy:** `infra/deploy-phaseT2.sh` (ingest), then `infra/deploy-phaseW1.sh` (web).

## Known limits (accepted)

- **Delivery is at least once.** A Firehose retry can store a batch twice, as for metrics. Each event's `id` is kept as `aws.log.event.id`, so duplicates can be spotted.
- **Delay.** Logs arrive about 1–2 minutes late because of Firehose buffering. Lower buffering means more, smaller requests.
- **No backfill.** A subscription only sends new events. Older logs stay in CloudWatch.
- **Secrets in logs** reach Leasyd as they are, as with any log source. Drop rules or `FilterPattern` can keep them out.

## Decisions needed

1. **Default scope:** the template defaults to **a chosen list of log groups** (recommended: predictable cost, no surprises), with "all log groups" as an opt-in. Or default to all?
2. **Lambda `START`/`END` lines:** keep them (recommended: simple and visible; a one-click drop rule removes them), or drop them by default? `REPORT` lines are kept either way, because they carry duration and memory.
3. **ECS service names:** use the container name (recommended: it matches what people see in the ECS console), or the log group name?
