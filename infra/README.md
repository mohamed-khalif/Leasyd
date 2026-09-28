# Infrastructure

## Phase 0: foundation

`phase0-foundation.yaml` creates the data bucket, the lifecycle rules, the `obs-boundary`
permissions boundary, the `obs-collector` / `obs-compaction` / `obs-query` roles and a
monthly budget.

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

After the tests pass, redeploy with `AllowTestAssume=false EnableLifecycleTest=false`
so the roles can only be assumed by their AWS services.

The bucket is retained if the stack is deleted, so data is never removed by accident.

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
