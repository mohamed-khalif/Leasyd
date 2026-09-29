# Infrastructure

## Bring it up, take it down

The whole platform is CloudFormation: one stack per part, deployed in order by one script.
Run these in CloudShell (admin credentials; Node.js and Python are already there):

```bash
export AWS_DEFAULT_REGION=us-east-1
infra/up.sh                     # deploy or update everything; safe to re-run
infra/down.sh                   # take it offline; data, tenants, keys and logins are kept
infra/up.sh                     # back again: existing customers' keys work as before
infra/down.sh --delete-data     # delete everything, data included (asks for the account id)
```

Options for `up.sh`: `ALERT_EMAIL=you@example.com` (alarms and budget; kept after the first run),
`DOMAIN=leasyd.com` (the product's own names, below), `TEST_TOOLS=1` (load generator and Athena
workgroup, for dev accounts). On a first run, confirm the alarm subscription email AWS sends.

| Stack | What | `down.sh` | `down.sh --delete-data` |
|---|---|---|---|
| `obs-dns` | the domain's DNS zone and certificate | kept | kept |
| `obs-state` | data bucket, index/usage/tenant tables, login pool | kept | deleted |
| `obs-phase0` | IAM roles and boundary, artifacts bucket, budget | deleted | deleted |
| `obs-phase2`, `3`, `4` | compaction and fast lane, index lookups, query engine and API | deleted | deleted |
| `obs-phaseT2`, `T5`, `T7` | ingest API, tenant operations, canary and alarms | deleted | deleted |
| `obs-phaseS1` | synthetic checks (scheduler, runner, browser, portal API) | deleted | deleted |
| `obs-phaseS1-build` | the browser-check image (ECR, CodeBuild) | deleted | deleted |
| `obs-phaseW1` | web app (S3 + CloudFront) | deleted | deleted |
| `obs-phase1`, `obs-phaseT6` | test tools (`TEST_TOOLS=1`) | deleted | deleted |
| `obs-phaseD1` | live demo data for the `leasyd-demo` tenant (`DEMO=1`) | deleted | deleted |

Outside CloudFormation, by design: each tenant's Firehose streams and API keys (made by the tenant
admin Lambda when a tenant is created) and the canary's key (SSM `/obs/canary/api-key`). `down.sh`
keeps them; `up.sh` reconnects them to the recreated API (`infra/tenant.sh restore`).
`--delete-data` deletes them.

### The product's domain

Only `ingest.<domain>` and `app.<domain>` are handed to AWS; the domain itself (website, email)
stays with its current DNS provider. `infra/deploy-dns.sh leasyd.com` (or
`DOMAIN=leasyd.com infra/up.sh`) creates a zone for each name and prints 8 NS records (4 per name)
to add at that provider, once. When public DNS shows them (minutes to hours), run
`infra/deploy-dns.sh` again: it issues the certificate. Then `infra/deploy-phaseT2.sh` and
`infra/deploy-phaseW1.sh` (or `infra/up.sh`) serve the API at `https://ingest.<domain>` and the web
app at `https://app.<domain>`. These names stay the same across `down.sh`/`up.sh`; the
AWS-generated URLs do not.

## Phase 0: foundation

`phase0-foundation.yaml` creates the `obs-boundary` permissions boundary, the `obs-collector` /
`obs-compaction` / `obs-query` roles, the artifacts bucket and a monthly budget. The data bucket and
its lifecycle rules are in `obs-state` (`infra/state.yaml`).

Deploy:

```bash
aws cloudformation deploy \
  --stack-name obs-phase0 \
  --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags project=obs phase=0
```

Test:

```bash
infra/phase0-test.sh obs-phase0              # IAM allow/deny checks + lifecycle probe upload
infra/phase0-test.sh obs-phase0 lifecycle    # 1-2 days later: probe should be GLACIER_IR
```

The tests need `AllowTestAssume=true` on `obs-phase0` and `EnableLifecycleTest=true` on
`obs-state`; both default to false, so the roles can only be assumed by their AWS services.

The data bucket is retained if a stack is deleted, so data is never removed by accident.

## Phase 1: write path

`phase1-write-path.yaml` creates a small VPC (public subnets, no NAT, S3 gateway endpoint),
an ECS cluster running the OpenTelemetry Collector (0.161.0) on Fargate, a `telemetrygen`
load-generator task, and the `obs.raw_logs` Glue table + `obs` Athena workgroup.
Phase 0 must be redeployed first: it now enables S3 request metrics on `_incoming/`.

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=0
aws cloudformation deploy --stack-name obs-phase1 --template-file infra/phase1-write-path.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=1
```

Since Phase T2, ingest is serverless (see below) and the collector has been removed from this
stack; it now holds only the load generator and the Athena tables. Phase 1's original test,
which sent traffic straight to a collector task, was retired; `infra/phaseT2-test.sh` covers
ingest.

## Phase 2: compaction

`phase2-compaction.yaml` creates the `obs-index` table, the `obs-compaction-dispatcher`
(every 15 min) and `obs-compaction-worker` Lambdas, an `obs-compaction-stuck` alarm
emailing AWSkhalif@gmail.com, and the `obs.logs` Athena table over the Parquet.
Code and tests are in `services/compaction/`
(`pip install -r requirements-dev.txt && pytest`).

Phase 0 must be redeployed first: it adds the artifacts bucket and lets the compaction
role delete index items and invoke the worker. The deployer also needs
`infra/iam/deployer-phase2.json`.

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=DISABLED
infra/phase2-test.sh load 3          # then wait until one full hour has closed (+10 min)
infra/phase2-test.sh compare
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
```

Confirm the SNS subscription email so the stuck-compaction alarm can reach you.

## Phase 3: index lookup

Compaction now builds a bloom filter per Parquet file over `trace_id` and `request.id`
(set `BloomAttributes` on the Phase 2 stack to change the attributes), and records every
service seen. `phase3-index.yaml` adds `obs-index-lookup`, a Lambda that returns the files
a query needs for given services, a time range and an optional trace or request ID. It
shares the Lambda bundle in `services/compaction/`.

```bash
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phase3.sh
infra/phase3-test.sh
```

Files compacted before this change have no bloom filter; lookups never skip them.

## Phase T1: tenant-aware storage, compaction and lookups

Every path and index key now starts with a tenant (see `services/compaction/layout.py`):
raw data under `_incoming/tenant=<T>/`, Parquet under `data/tenant=<T>/`, index keys
`<T>#...`. The query role no longer reads data itself; lookups assume `obs-tenant-reader`
with the tenant as a session tag, and IAM limits that session to the tenant's prefix and
index keys. Until authenticated ingest (T2), the collector files everything under tenant
`default`. Athena tables now need `WHERE tenant = '...'`.

Deploy in this order. **Phase 0 must be deployed with admin credentials**: it changes the
`obs-boundary` permissions boundary, which `obs-deployer` is deliberately not allowed to
modify. Also attach `infra/iam/deployer-phaseT.json` to `obs-deployer` for the tests.

```bash
AWS_PROFILE=<admin> aws cloudformation deploy --stack-name obs-phase0 \
  --template-file infra/phase0-foundation.yaml --capabilities CAPABILITY_NAMED_IAM
aws cloudformation deploy --stack-name obs-phase1 --template-file infra/phase1-write-path.yaml \
  --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phase3.sh
infra/phase0-test.sh obs-phase0      # now includes cross-tenant deny checks
infra/phase3-test.sh                 # lookups, including tenant isolation
```

Data written before this change (`logs/`, `_incoming/logs/`, index keys without a tenant)
is no longer read or compacted; it's test data and can be deleted.

## Phase T2: authenticated, serverless ingest

Customers send OTLP/HTTP to a public HTTPS endpoint with an `x-api-key` header:

```
OTEL_EXPORTER_OTLP_ENDPOINT=<IngestEndpoint output of obs-phaseT2>
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=<key>
```

```
SDK --HTTPS--> API Gateway --> authorizer Lambda (key -> tenant, obs-tenants table)
                   |              usage plan: per-tenant rate limit
                   v
               obs-ingest Lambda --> Firehose obs-t-<tenant>-<signal> --> s3://.../_incoming/tenant=<T>/<signal>/
```

- Nothing runs, or costs, while idle. Cost is per request (API Gateway, Lambda) and per GB
  (Firehose, $0.029/GB; each record is billed as at least 5 KB).
- The tenant comes only from the authorizer; client `obs.*` resource attributes are stripped.
- A client gets 200 only once Firehose has stored the records durably; otherwise 503, and its
  SDK retries (at least once: a retry after a partial failure can duplicate records).
- Each tenant has its own Firehose stream per signal (created by `infra/tenant.sh`), so no
  dynamic-partitioning fees or shared partition limits, and per-tenant throughput limits.
  Streams flush every 30 s (`BUFFER_SECONDS`) or at 64 MB.
- Requests over about 4.5 MB are rejected (Lambda's 6 MB limit applies after API Gateway base64-encodes the body); SDK batches are far smaller.

**Phase 0 must be redeployed with admin credentials** (the boundary gains Firehose puts and
Firehose's S3 delivery actions). Attach `infra/iam/deployer-phaseT2.json` to `obs-deployer`. Then:

```bash
AWS_PROFILE=<admin> aws cloudformation deploy --stack-name obs-phase0 \
  --template-file infra/phase0-foundation.yaml --capabilities CAPABILITY_NAMED_IAM
aws cloudformation deploy --stack-name obs-phase1 --template-file infra/phase1-write-path.yaml \
  --capabilities CAPABILITY_NAMED_IAM          # removes the old collector
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phaseT2.sh
infra/phaseT2-test.sh
```

Onboard a tenant: `infra/tenant.sh create acme` (creates its streams, prints the key once).
Revoke: `infra/tenant.sh revoke acme`.

## Phase T3: fast lane (searchable within about a minute)

Each new raw file (Firehose flushes every 30 s) triggers `obs-recent-indexer` through an S3
event on EventBridge. It indexes the file as `kind=raw` entries, one per (service, event
hour) with time range, row count and bloom filter, using the same parsing code as compaction.
`obs-index-lookup` returns raw and Parquet files together, marked by `kind`.

Handover: a chunk's compaction plan decides which copy lookups show. While the plan is
`planned`, raw entries are visible and its new Parquet entries hidden; once it's `committed`
(a single write), Parquet is visible and those raw entries hidden; cleanup then deletes the raw
entries and files. So a query never counts a record twice or misses it, whatever step a crash
interrupts (tested in `services/compaction/test_fastlane.py`).

Plan records moved to `<T>#_plan#...` so a tenant's lookups can read them; the tenant reader
can now also read that tenant's raw files. No plans may be in flight when deploying this
(`aws dynamodb scan --table-name obs-index --filter-expression 'begins_with(pk, :p)'
--expression-attribute-values '{":p":{"S":"_plan#"}}' --select COUNT` should be 0).

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phase3.sh
infra/phaseT3-test.sh
```

## Phase T4: traces and metrics

Compaction, the fast lane and lookups now cover all three signals (`COMPACT_SIGNALS`,
default `logs,traces,metrics`). Each becomes Parquet under `data/tenant=<T>/<signal>/`, with
one Athena table per signal (`WHERE tenant = '...'` as for logs):

- `obs.traces`: one row per span, at its start time: name, kind, status, ids, duration,
  attributes, and nested `events` and `links`. Bloom filters hold trace IDs (and the
  `BloomAttributes` IDs), so a trace ID lookup opens only the files holding that trace.
- `obs.metrics`: one row per data point. Gauge, sum, histogram, exponential histogram and
  summary points share the table; `metric_type` says which columns are set (`value` for
  gauge and sum; `count`, `sum`, buckets or quantiles for the rest). Rows are sorted by
  metric, then time. Metric points carry no IDs, so they have no bloom filter (exemplars are
  not stored yet).

Lookups take `"signal": "traces"` or `"metrics"`. Each signal has its own stuck-compaction
alarm (`obs-compaction-stuck`, `-traces`, `-metrics`).

```bash
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/phaseT4-test.sh
```

## Phase T5: tenant operations

`obs-tenant-admin` (a Lambda, `services/tenants/admin.py`) is the control plane; `infra/tenant.sh`
wraps it:

```bash
infra/tenant.sh create acme            # streams + first API key (printed once)
infra/tenant.sh rotate acme 24         # new key; old keys keep working for 24 h, then are refused
infra/tenant.sh revoke acme [key-id]   # refuse one key, or all
infra/tenant.sh usage acme 2026-09-01 2026-09-30
infra/tenant.sh status acme
infra/tenant.sh delete acme            # everything the tenant has, see below
infra/tenant.sh list
```

- **Usage:** each compacted chunk writes one record to `obs-usage` (records, raw bytes received,
  Parquet bytes stored), before its commit and under a key fixed by the chunk, so a re-run
  never counts twice. Usage appears once an hour is compacted (about an hour behind).
  Bytes scanned by queries will be metered by the query engine (Phase 4).
- **Rotation:** the authorizer accepts a rotated key until its `expires_at`; the sweep
  (every 15 min) then disables it in API Gateway too.
- **Deletion:** keys are refused at once (within API Gateway's 1-minute cache), streams deleted,
  then a purge pass deletes all of the tenant's objects (`_incoming/`, `data/`, error files)
  and index entries. Passes repeat 20 minutes apart (longer than a compaction worker's lease)
  until one finds nothing, so data a worker was still writing is caught; then the tenant is
  `deleted` and its id can be reused. Usage records are kept (billing).

Deploy (the boundary changes, so **Phase 0 needs admin credentials**; attach
`infra/iam/deployer-phaseT5.json` to `obs-deployer` first):

```bash
AWS_PROFILE=<admin> aws cloudformation deploy --stack-name obs-phase0 \
  --template-file infra/phase0-foundation.yaml --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phaseT2.sh
infra/deploy-phaseT5.sh
infra/phaseT5-test.sh      # ~30-45 min: waits for a deletion to finish (QUICK=1 skips that wait)
```

## Phase T6: scale tests

Test tooling only (`obs-phaseT6`: the `obs-loadgen` Lambda and `obs-loadtest` results table;
delete the stack when T6 is done). `infra/t6/loadtest.py` drives it:

- 100 tenants `t6-000..t6-099` with Zipf-distributed volumes (the largest sends 100x the smallest;
  2-20 services each). Keys are kept in `~/.obs-t6-keys.json`, outside the repo.
- Senders post realistic OTLP protobuf (gzip, 512-item batches, retries like an SDK) through the
  public endpoint. A prober sends a marked log record per sampled tenant every minute and
  times how long until a lookup finds it (freshness), and times 1 h / 24 h lookups.
- The report covers achieved GB/h, HTTP statuses, retries, drops, freshness percentiles,
  lookup speed, Lambda/API Gateway/Firehose/DynamoDB metrics and cost per GB at list prices.

Attach `infra/iam/deployer-phaseT6.json` to `obs-deployer` (read-only limits and metrics), then:

```bash
infra/deploy-phaseT6.sh
python3 infra/t6/loadtest.py preflight
python3 infra/t6/loadtest.py tenants create --n 100
python3 infra/t6/loadtest.py run --gbph 1 --minutes 45 --step s1 --yes
python3 infra/t6/loadtest.py report --step s1
python3 infra/t6/loadtest.py compaction --since-minutes 180   # after the hours close
python3 infra/t6/loadtest.py tenants delete                   # at the end of T6
```

### T6 fixes (no load needed)

- Bloom filters over 1 KB live in S3, not in index items, so time-range lookups read small items.
  Lookups query services and fetch blooms in parallel.
- **Day filters** (`services/compaction/dayfilter.py`): compaction writes each chunk's ID digests;
  `obs-day-sealer` (hourly, 2 h after a day ends) builds one sharded filter per tenant, signal and
  day. An ID lookup reads one small range per day (tens of KB) and skips every file of a day that
  can't hold the ID. A day filter is used only while no chunk was added to the day after sealing
  (`dirty == sealed`), so late data is never missed; it's re-sealed on the next run. Digests are
  deleted 3 days after the day; later late data leaves that day on per-file blooms.
- Dispatcher lists tenants in parallel (timeout 5 min). Fast-lane bloom files are deleted with
  their entries.

Deploy: Phase 0 (role policy only; no boundary change), then Phase 2 and Phase 3.

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false
infra/deploy-phase3.sh
```

## Phases 4-5: query engine

`obs-query` answers a JSON query over one tenant's data (`services/compaction/query.py` documents
the format): filters (`where`), an ID `match`, `group_by` with `count/sum/min/max/avg/p50/p90/p95/p99`,
or `search` for the newest matching rows. It runs the index lookup, splits the files into chunks of
similar size (~256 MB each, up to 64), runs an `obs-query-worker` per chunk in parallel, and merges
their partial results. Workers download files with tenant-scoped credentials (IAM refuses anything
outside the tenant) and query them with DuckDB; raw (fast-lane) files are parsed exactly as
compaction would. Parquet is read in place by default: only the footer and the columns the query
needs, as S3 range requests (`ReadMode=download` fetches whole files instead). Each worker takes
~64 MB of files (`BytesPerWorker`), so the number of workers grows with the data a query covers. Queries are never SQL from the caller: fields are checked, values are bound.

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM          # lets obs-query invoke its workers
infra/deploy-phase4.sh
python3 infra/phase4-test.py                   # over the T6 load-test tenant t6-000
```

Example:

```bash
aws lambda invoke --function-name obs-query --cli-binary-format raw-in-base64-out --payload '{
  "tenant": "t6-000", "signal": "logs", "start": "2026-09-27T00:00:00Z", "end": "2026-09-28T00:00:00Z",
  "where": [{"field": "severity_number", "op": ">=", "value": 17}],
  "group_by": ["attributes.http.route"], "aggs": [{"fn": "count"}]}' /dev/stdout
```

### T6: compress before Firehose

Firehose bills the bytes it receives, and ingest sent it uncompressed OTLP JSON (2.1x the protobuf
customers send), so Firehose was about half the cost. Now ingest can gzip each record
(`RecordCompression=gzip` on obs-phaseT2) and tenant streams pass records through
(`CompressionFormat: UNCOMPRESSED`); the S3 object is gzip members back to back, a valid `.gz`.
Every raw-file reader accepts all encodings of the changeover (Firehose-gzipped, per-record gzip,
plain, and gzip-in-gzip), so no step can break reads. Roll out in this order:

```bash
infra/deploy-phase2.sh --parameter-overrides ScheduleState=ENABLED AllowCrashInjection=false   # readers
infra/deploy-phase4.sh --parameter-overrides BytesPerWorker=67108864                           # query readers
infra/deploy-phaseT5.sh                                                                        # new streams pass through
python3 infra/migrate-stream-compression.py --apply                                            # existing streams
infra/deploy-phaseT2.sh --parameter-overrides RecordCompression=gzip                           # ingest compresses
```

Proof that customers see no difference: `infra/compression-check.py send before` (before the last
step), `send after` (after it), then `compare`, which reads both batches back through `obs-query`
and requires every field of every record to match. Locally, tests require identical Parquet (every
column, and the blooms) for all four encodings, and for the same requests with compression off and on.

## Phase T7: failure visibility

Every alarm emails the `obs-alerts` topic (Phase 2; subscription AWSkhalif@gmail.com, confirmed).

| Alarm | Fires when | Where |
|---|---|---|
| `obs-fastlane-failed` | a raw file failed fast-lane indexing after 2 retries (within 5 min); it is searchable only once compacted | Phase 2 (queue `obs-fastlane-failed`) |
| `obs-compaction-stuck[-traces/-metrics]` | raw data older than 95 min (normally <= ~80), or the dispatcher stopped | Phase 2 |
| `obs-compaction-worker-errors`, `obs-compaction-dispatcher-errors`, `obs-day-sealer-errors` | any error in 15 min | Phase 2 |
| `obs-canary-logs`, `obs-canary-traces` | the canary's record is not findable 2 min after sending, 3 minutes in a row (or the canary is not running) | T7 |
| `obs-ingest-5xx`, `obs-ingest-errors`, `obs-ingest-authorizer-errors` | 5+ in 5 min | T7 |
| `obs-query-errors`, `obs-query-worker-errors`, `obs-index-lookup-errors` | 3+ in 5 min | T7 |

The canary (`obs-canary`, every minute) sends one log record and one span for the `canary` tenant
through the real endpoint and checks the pair sent 2 minutes earlier; metrics `obs/CanaryMissing`
and `obs/CanarySendFailed` per signal. Its API key is in SSM `/obs/canary/api-key` (SecureString).

Deploy (attach `infra/iam/deployer-phaseT7.json` to `obs-deployer` first; Phase 0 with admin
credentials, since the boundary gains `sqs:SendMessage` and `ssm:GetParameter` on `obs-*` / `/obs/*`):

```bash
AWS_PROFILE=<admin> aws cloudformation deploy --stack-name obs-phase0 \
  --template-file infra/phase0-foundation.yaml --capabilities CAPABILITY_NAMED_IAM
infra/deploy-phase2.sh
infra/deploy-phaseT7.sh          # first run creates the canary tenant and stores its key
```

Failed fast-lane files: `python3 infra/redrive-fastlane.py` lists them, `--apply` replays them.

Proven on AWS (2026-09-28 21:02, one injected failure each): an invalid compaction job ->
`obs-compaction-worker-errors` in ~1.5 min; the canary tenant's Firehose buffer set to 900 s ->
`obs-canary-logs/traces` in ~4.5 min; a corrupt raw file -> 3 failed fast-lane attempts -> queue ->
`obs-fastlane-failed` in ~5.5 min, then listed and replayed with the redrive script (file removed:
skipped, message deleted). All alarms back to OK after the fixes.

## Phase Q1: customer query API

`POST <IngestEndpoint>/v1/query` with header `x-api-key: <read key>` and a JSON query (the fields of
`services/compaction/query.py`: `signal`, `start`, `end`, `services`, `where`, `match`, `group_by`,
`aggs`, `search`, `order`, `limit`). The tenant comes only from the key; anything else in the body is
ignored. Errors: 400 bad query, 401 no/unknown key, 403 wrong key scope, 413 result too large,
504 over API Gateway's 29 s. A new key can be refused by some requests for up to ~10 minutes.

Tested on AWS (2026-09-28, `infra/query-api-test.py`): same counts as the engine for a day of the
largest tenant in ~4 s; a tenant named in the body is ignored; read keys can't send, ingest keys
can't query; 401 without a key; 400 for bad or incomplete queries; trace id across 30 days ~2 s.

Keys have a scope. `ingest` keys (every key from `create` / `rotate`) may only send data; `read` keys
may only query, so a key embedded in an application can't read data back:

```bash
infra/tenant.sh read-key <tenant>        # prints a new read key once
infra/tenant.sh revoke <tenant> <key-id> # revoke one key
curl -s -X POST "$ENDPOINT/v1/query" -H "x-api-key: $READ_KEY" -H 'Content-Type: application/json' \
  -d '{"signal":"logs","start":"2026-09-28T00:00:00Z","end":"2026-09-28T23:59:59Z",
       "where":[{"field":"severity_number","op":">=","value":17}],
       "group_by":["service"],"aggs":[{"fn":"count"}]}'
```

Deploy (obs-phase4 first: it creates `obs-query-api`, which obs-phaseT2 routes to):

```bash
infra/deploy-phase4.sh --parameter-overrides BytesPerWorker=67108864
infra/deploy-phaseT2.sh
infra/deploy-phaseT5.sh     # tenant admin: read-key, scope-aware rotate
infra/deploy-phaseT7.sh     # alarm obs-query-api-errors
python3 infra/query-api-test.py
```

## Phase U1: customer logins

People sign in to the product with a login (Cognito user pool `obs-users`, now in `obs-state`);
machines keep using API keys. Each user belongs to exactly one tenant, in the immutable attribute
`custom:tenant` that only the tenant admin sets. There is no self sign-up.

```bash
infra/tenant.sh invite-user <tenant> <email>   # emailed a temporary password (Cognito's sender: ~50/day; SES later)
infra/tenant.sh users <tenant>
infra/tenant.sh remove-user <tenant> <email>   # signs them out everywhere, deletes the login
```

Deleting a tenant removes its logins too. The web app signs users in with the `obs-app` client
(SRP or password; ID token valid 1 h, refresh token 30 days) and calls, with header
`Authorization: <ID token>`:

- `GET /v1/app/me`: `{"tenant": ..., "email": ...}`
- `POST /v1/app/query`: the same query body as `/v1/query`; the tenant is the token's `custom:tenant`.

These routes take no API key, so users are limited by the stage throttle, not a usage plan.

Deployed by `infra/up.sh` (the pool is in `obs-state`; `obs-phaseT2` adds `/v1/app/*`, `obs-phaseT5`
invite-user / remove-user / users). Test: `python3 infra/login-test.py` (as `obs-deployer`, with
`infra/iam/deployer-phaseU1.json` attached for its test users).

Tested on AWS (2026-09-29): an invited user signs in; `/v1/app/me` names their tenant;
`/v1/app/query` gives the engine's counts for that tenant only (~1.8 s for a day of the largest
tenant), ignoring a tenant in the body; no token -> 401, a token edited to claim another tenant ->
refused, an API key -> 401; the user can't change their tenant; after remove-user they can neither
sign in nor refresh.

## Phase W1: Leasyd web app

`services/web` (React + TypeScript + Vite; SVG charts, no chart library) is served by CloudFront
from a private S3 bucket (`obs-phaseW1`). CloudFront also routes `/v1/*` to the API, so the app and
the API share one origin: no CORS, same authorizers. Screens: Telemetry Insights and Usage & Cost
dashboards, Logs explorer, Trace view, sign-in (Cognito SRP, first-login password change). The app
reads its settings (`region`, `userPoolId`, `clientId`, `apiBase`) from `/config.json`, written by
the deploy script.

```bash
# attach infra/iam/deployer-phaseW1.json to obs-deployer first
infra/deploy-phase4.sh --parameter-overrides BytesPerWorker=67108864   # query API: time buckets for the charts
infra/deploy-phaseW1.sh                              # prints the app's URL
infra/tenant.sh invite-user <tenant> <your email>    # a login; the email has a temporary password
```

Local work without AWS: `cd services/web && npm install && npm run mock` (a seeded fake backend,
no sign-in) at http://localhost:5173. Placeholder prices for Usage & Cost: `services/web/src/pricing.ts`.

## Phase D1: demo tenant

`obs-demo` (`services/demo/demo.py`) sends a realistic online shop's telemetry for the `leasyd-demo`
tenant through the real ingest endpoint every minute: a dozen services calling each other (one
trace per request), logs tied to spans, metrics of every kind, daily traffic, ~3% declined
payments and a 12-minute shipping incident every 3 hours. About 50 MB a day at peak.

```bash
infra/deploy-phaseD1.sh                             # first run: tenant, key in SSM, 24 h backfill
infra/tenant.sh invite-user leasyd-demo <email>     # a login to look at it
BACKFILL_HOURS=6 infra/deploy-phaseD1.sh            # backfill again (e.g. after a pause)
infra/deploy-phaseD1.sh --parameter-overrides State=DISABLED   # pause
```

## Phase S1: synthetic checks (HTTP and browser)

Customers set up checks in the portal (Monitoring > Synthetics). A check is 1-10 steps run in
order every 1, 5, 15, 30, 45 or 60 minutes from us-east-1; the first failing step ends the run.
At most 20 checks per tenant (`obs-tenants` items `check#<tenant>#<id>`).

**HTTP checks** (`services/synthetics/synthetics.py`): each step is a request.
- Variables: `{name}` in a step's URL, headers, body, auth or constraint values, from the check's
  variables, its secrets, or values extracted by earlier steps (JSON path such as
  `data.items[0].id`, a regex's first group, or a response header). Cookies carry over.
- Auth: basic or bearer; the password or token must be a secret or an extracted value.
- Constraints: status (`<400`, `2xx`, `3xx, 404, 406-410, >=500`), response time, body contains /
  not contains / regex (time-limited), header, JSON value, TLS certificate days left. Options:
  follow redirects, accept any certificate, don't record the response when it fails.

**Browser checks** (`services/synthetics/browser.py`): headless Chromium (Playwright) in one tab.
- Steps: open a URL (fails on a 4xx/5xx page), click, hover, type (variables and secrets), choose
  an option, press a key, wait for an element, wait, check text is / isn't shown, check an element,
  check the URL, save an element's text or attribute as `{name}`. Elements by CSS selector or
  `text=...`; each step waits up to 15 s (settable) for its element. Whole check: up to 60 s
  (18 s for "Test" and "Run now", which must fit an API call).
- Desktop (1366x768) or mobile (390x844, touch). Screenshots when a step fails, or after every
  step: JPEGs under `synthetics/tenant=<T>/<check>/<run>/<step>.jpg` in the data bucket, kept 30
  days, deleted with the tenant, shown through `GET /v1/app/checks/{id}/screenshot?run=&step=`.
- Per page opened: first byte, first and largest contentful paint, load, layout shift; console
  errors, responses with errors, failed and blocked requests, reported with the step.
- `obs-synthetics-browser` is a container image (`services/synthetics/Dockerfile`, arm64) built by
  CodeBuild (`obs-phaseS1-build`, ECR `obs-synthetics-browser`); `deploy-phaseS1.sh` rebuilds it
  only when its source changes (about 5 minutes).

**Results** are the tenant's own telemetry: a trace per run (the check, a span per step; service
`synthetics`; trace id = run id), metrics `synthetics.check.success` / `duration`,
`synthetics.step.duration`, `synthetics.check.tls_days_remaining`, `synthetics.browser.ttfb` /
`fcp` / `lcp` / `load` / `cls`, and an ERROR log when it fails (first 2 KB of the response, or the
browser's errors; secrets and extracted values masked).

**Functions**: `obs-synthetics-api` serves `/v1/app/checks` (routed by `obs-phaseT2`, Cognito: the
tenant is the user's). `obs-synthetics-tick` (every minute) hands due checks to
`obs-synthetics-run` in batches (25 HTTP, 5 browser); for a browser check the runner decrypts its
secrets and invokes `obs-synthetics-browser`, then records the result.

**Safety**
- Only public addresses. HTTP: each step's final URL is resolved and that address used; private,
  loopback, link-local (169.254.169.254) and other non-public addresses are refused, redirects and
  variables included. Browser: every connection the page makes (page, images, scripts, XHR,
  WebSockets) goes through a proxy inside the browser function that does the same check;
  Chromium sends loopback through it too, and QUIC and non-proxied WebRTC are off.
- Secrets: encrypted with the KMS key `alias/obs-checks` under the context `{tenant, check}` (a
  ciphertext only decrypts for its own check), write-only in the API, masked in every result
  (in screenshots, fields and text showing one are painted over).
- No customer code runs (browser steps are a fixed list of actions). The browser function's role
  can only write its own logs: Chromium runs without its sandbox in Lambda, so a page that took it
  over must find nothing worth taking. After each run every other process is killed and /tmp
  emptied. The runner's role can decrypt check secrets, put records on tenant streams, invoke the
  browser and save screenshots; the API's can also read them.

```bash
aws cloudformation deploy --stack-name obs-phase0 --template-file infra/phase0-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM      # boundary: the checks' KMS key and the image build
infra/deploy-phaseS1.sh      # before T2: builds the browser image, then the functions
infra/deploy-phaseT2.sh      # /v1/app/checks routes (and .../screenshot)
infra/deploy-phaseT5.sh      # deleting a tenant removes its checks and screenshots
infra/deploy-phaseW1.sh      # the Synthetics pages
```
