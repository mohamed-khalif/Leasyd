# Phased build plan: S3-native observability platform (rev. 2)

26 Sept 2026 · @Mohamed

The platform separates two concerns that traditional observability tools bundle together: **storage** (cheap, durable, S3) and **compute** (expensive, spun up only when a question is actually asked). Each phase builds one layer of that separation and is tested in isolation on AWS before the next layer depends on it.

## What changed from rev. 1

| # | Change | Why |
|---|---|---|
| 1 | Dropped Kinesis | Nothing read from it. The collector sends metrics straight to the hot path and data straight to S3. |
| 2 | Raw data now lands in its own top-level `_incoming/` prefix. Compacted data goes under `logs/dt=/hour=/service=/` (time first). | One consistent layout. It also makes IAM and lifecycle rules a single prefix each. |
| 3 | Collector writes OTLP JSON. Compaction converts it to Parquet. | The collector's S3 exporter can't write Parquet. |
| 4 | Index sort key is `min_ts#file_id`, and each file spans at most one hour | Range queries now find files that overlap the start of the range. Two files with the same `min_ts` can no longer overwrite each other. |
| 5 | Compaction uses predictable file names, in the order write file → write index → delete originals | Re-running a crashed job doesn't create duplicates or files the index can't find. |
| 6 | Added `size_bytes` and `storage_class` to the index. Bloom filters are stored inline or in S3 depending on size. | Needed for Phase 7 cost estimates. Keeps items under DynamoDB's 400 KB limit. |
| 7 | Older data moves to Glacier **Instant Retrieval**. Lifecycle rules apply only to `logs/`. | Old data stays queryable. Small raw files never hit IA's 128 KB minimum charge. |
| 8 | Result cache is DynamoDB with TTL (time-to-live), not ElastiCache | ElastiCache Serverless has a monthly minimum and never scales to zero. |
| 9 | Hot path is VictoriaMetrics, fed by the collector's `prometheusremotewrite` exporter | Says how metrics get to the hot path. |
| 10 | The Glue reference, the garbled latency sentence and the undefined scan-progress indicator are fixed | Consistency fixes. |
| 11 | New section: data freshness | Cold-path data can't be queried until it's compacted. The plan now says so. |

## Architecture-to-AWS mapping

| Component | AWS service |
|---|---|
| Agents/collectors | Customers' OpenTelemetry SDKs (or their own collectors) send OTLP/HTTP with an API key |
| Cold-path writer | API Gateway → ingest Lambda → a Firehose stream per tenant and signal, writing OTLP JSON to `_incoming/tenant=<T>/` (Phase T2) |
| Cold storage | S3 |
| Compaction | Lambda: a dispatcher runs on an EventBridge schedule and starts one worker per partition |
| Metadata index | DynamoDB (on-demand) |
| Query-time compute | DuckDB inside Lambda, run in parallel via a Step Functions Map state |
| Hot path (alerting) | VictoriaMetrics + vmalert + Alertmanager on a small EC2 instance |
| Result cache | DynamoDB with TTL; results over 400 KB go to S3 with a pointer |
| Query frontend | Custom React UI + API Gateway, plus a Grafana datasource plugin |

**Bucket layout:**

```
s3://bucket/
  _incoming/{signal}/dt=YYYY-MM-DD/hour=HH/{signal}_<uuid>.json.gz             # raw, short-lived, arrival time
  {signal}/dt=YYYY-MM-DD/hour=HH/service=X/part-<hash>-NNN.parquet           # compacted
  _results/<query-hash>.parquet                                              # large cached results
```

`{signal}` is `logs`, `traces` or `metrics`. No part of the path uses a field with unbounded cardinality (like user IDs).

Raw files are partitioned by **arrival time only**. The collector's S3 exporter names each file after the first resource in a batch, so a `service=` partition there could file one service's records under another's name. Compaction splits by service and by event time.

**Kinesis:** add it back only if a second real-time consumer of the raw stream appears, such as streaming anomaly detection. Until then it's cost with no benefit.

Every phase has a build step and a test on AWS. Nothing counts as working until it has been measured against real S3 data.

## Phase 0: AWS foundation

**Build**
- A separate AWS account (or tightly scoped OU) for the platform, isolated from production billing.
- S3 bucket with lifecycle rules from day one:
  - On `logs/`, `traces/` and `metrics/`: move to Standard-IA after 30 days and to Glacier Instant Retrieval after 180 days. Instant Retrieval keeps old data queryable in milliseconds. Flexible Retrieval and Deep Archive would break queries.
  - On `_incoming/`: **no** transitions and no expiration. A stuck compaction must never silently lose data. Phase 2 adds an alarm for that case instead.
- IAM roles per component:
  - **Collector:** `s3:PutObject` on `_incoming/*` only.
  - **Compaction:** read and delete on `_incoming/*`, write on `logs/*`, `traces/*` and `metrics/*`, write to the DynamoDB index.
  - **Query:** read on `logs/*`, `traces/*` and `metrics/*`, read the DynamoDB index, read and write the result cache.
- AWS Budgets: an account-wide alert at $50, plus per-phase alerts using cost-allocation tags (`phase=N`).

**Test on AWS**
- From each role, try an action it shouldn't be allowed and confirm it's denied: the query role writing, the collector reading, the collector writing outside `_incoming/`.
- Upload a dummy object and confirm the lifecycle transition, using a shortened test rule.

## Phase 1: Write path

**Build**
- Run the OTel Collector as an ECS Fargate task, receiving OTLP from a test service.
- Configure the `awss3exporter` with the `otlp_json` format and gzip, batching into `_incoming/{signal}/dt=/hour=/`.

**Test on AWS**
- Point a load generator at the collector. Confirm files land under the right prefixes with the right partition values.
- Create a Glue table over `_incoming/logs/` using the JSON SerDe with the nested OTLP schema. Query it in Athena, flattening the nested records with `UNNEST`. This proves the data is readable and the schema is right before any custom query layer exists.
- Record the bytes scanned and duration Athena reports. This is the **raw-JSON baseline** for Phase 2.
- Check S3 request metrics in CloudWatch for per-prefix throttling.

## Phase 2: Compaction

**Build**
- **Dispatcher Lambda** (EventBridge, every 15 min):
  - Lists `_incoming/` hour partitions that closed at least 10 minutes ago. The 10-minute grace period catches late-arriving data.
  - Splits each closed hour's raw files into **chunks** (at most 2,000 files or 200 MB each) and records a **plan** per chunk in DynamoDB: its input keys and `batch_id = hash(input keys)`. Files already in a plan are never planned again.
  - Invokes one worker per plan, so a busy hour is compacted by many workers in parallel. Plans still present from an earlier run are invoked again, which is how a crashed chunk gets retried.
  - Holds a lease while planning, so two overlapping runs can't split the same files two different ways.
  - Publishes a CloudWatch metric for the age of the oldest `_incoming/` hour, with an alarm if it goes over 3 hours.
- **Worker Lambda**, per chunk:
  0. Take a lease on the chunk (a conditional DynamoDB write), so a retry never runs alongside the original.
  1. Read the chunk's inputs, split rows by service and by event hour, sort by timestamp, and write Parquet under each `{signal}/dt=/hour=/service=/`. A group bigger than 4 million rows (~150–250 MB) is cut into consecutive time slices, `part-<batch_id>-000.parquet`, `-001`, …, so no single file is too big for one query worker. The cut depends only on row count, so a re-run produces the same file names and overwrites instead of duplicating.
  2. Write the index entries (Phase 3), keyed by `min_ts#<batch_id>-NNN`. Re-writing them is harmless for the same reason.
  3. Commit: mark the plan committed. A crash before this redoes steps 1–2; a crash after it skips to step 4.
  4. Delete the input files, then the plan.
- Data that arrives after its hour was planned gets its own plan on the next run, and becomes additional files.
- Files never cross an hour boundary, so every file covers at most one hour. Phase 3's range lookup depends on this.

**Test on AWS**
- Run the Phase 1 Athena query against the compacted Parquet. Compare bytes scanned and duration with the raw-JSON baseline. This is the billed-in-dollars case for compaction.
- Kill a worker at each step boundary (after writing, indexing, committing, and part-way through deleting). Re-run it. Confirm row counts match and there are no duplicate files or index entries.
- Use S3 Storage Lens to confirm average object size goes up and object count goes down.

## Phase 3: Metadata index

**Build**
- DynamoDB table `obs-index` (created in Phase 2), on-demand billing:
  - **Partition key:** `signal#service`
  - **Sort key:** `min_ts#<batch_id>-NNN`
  - **Attributes:**
    - `max_ts`, `file_path`, `row_count`, `size_bytes`
    - `storage_class`: set when lifecycle rules move the file, for cost estimates
    - `bloom`, `bloom_m`, `bloom_k`, `bloom_n`, `bloom_fields`: a bloom filter over every `trace_id` and `request.id` value in the file (the attributes are configurable: `BloomAttributes` on the Phase 2 stack). Sized for 1% false positives, about 10 bits per distinct value. Stored inline if under 300 KB (~250,000 values); otherwise it goes to `{signal}/_bloom/` in S3 and the item holds `bloom_s3_key`.
  - A `_services#{signal}` item lists every service seen, so a lookup that names no service can search them all.
- The Phase 2 worker fills the index in the same pass, since it already reads every row.
- **Lookup Lambda** `obs-index-lookup` (runs as the read-only query role). Input: services (optional), start, end, and optionally `match` on a trace or request ID. Output: the files to read, plus how many candidates each step kept.
  - **Time range:** for `[t0, t1]`, query `min_ts BETWEEN (t0 − 1h) AND t1`, then filter `max_ts ≥ t0`. The one-hour lookback works because no file spans more than one hour. No S3 listing.
  - **ID match:** drop every file whose bloom filter says the ID is certainly absent. Files indexed before blooms existed are never dropped.
  - Phase 4 imports the same module rather than calling the Lambda, to save a network hop.

**Test on AWS** (`infra/phase3-test.sh`)
- Seed 3 hours × 3 services of logs, each record with its own random trace and request ID, and compact them.
- A time-range lookup for one service returns only the overlapping files, far fewer than an S3 listing of the same day.
- A file that starts before the range is included; one that ends before it is excluded.
- A trace ID or request ID lookup keeps about 1 of the 9 files, and it's the right one.
- Athena confirms that file really holds the ID (no false negatives).
- Report warm lookup latency and DynamoDB read units, and the table's consumed capacity at idle.

## Phase T: Multi-tenancy and production hardening

The platform will serve many separate customers (tenants). This phase comes before Phase 4, because it changes storage paths and index keys, and the query engine is where tenant isolation is enforced.

**Decisions**
- **Isolation:** one shared bucket and index; every path and index key starts with the tenant ID. Queries run under credentials that AWS restricts to the tenant's prefix and index keys (IAM session tags), so a bug in our code still cannot read another tenant's data.
- **Ingest:** a public HTTPS endpoint. Each customer's OpenTelemetry SDK sends OTLP/HTTP with an API key header. The key maps to a tenant ID; requests without a valid key are rejected. (OTLP/HTTP rather than gRPC, since API Gateway doesn't proxy gRPC; every OpenTelemetry SDK supports both.)
- **Scale:** elastic. The collector autoscales behind a load balancer, compaction and queries fan out across Lambdas, and DynamoDB is on-demand. Per-tenant rate limits and quotas stop one customer from starving the others.

**Targets** (volumes are unknown and will grow, so the aim is proving the design keeps scaling out, and measuring cost as it does)
- **Freshness:** a log line is searchable within **60 seconds** of arriving.
- **Search speed:** 1 day of one tenant's logs in **under 5 seconds**; a trace or request ID lookup across 30 days in **under 3 seconds**.
- **Tenants:** about 100, of mixed sizes.
- **Durability:** no accepted data is lost when a collector task, Lambda or availability zone fails.
- **Load test:** ramp 1 → 10 → 50 GB/hour across 100 tenants; at each step record throughput, freshness, search speed and cost per GB, and fix the first bottleneck before stepping up.

**Build, in order**
- **T1. Tenant-aware storage, compaction and lookups.** ✅ Deployed and tested on AWS.
  - Raw: `_incoming/tenant=<T>/<signal>/dt=/hour=/`. Compacted: `data/tenant=<T>/<signal>/dt=/hour=/service=/`. Lifecycle tiering moves to the `data/` prefix.
  - Index keys become `<T>#<signal>#<service>`; every internal record carries the tenant.
  - Lookups require a tenant and read through a tenant-scoped role (`obs-tenant-reader`) that IAM restricts to `data/tenant=<T>/*` and index keys starting `<T>#`.
- **T2. Authenticated, serverless ingest.** ✅ Deployed and tested on AWS: auth, tenant isolation, throttling, revocation (~1 min), 20,000 protobuf records stored exactly once, data in S3 ~20–35 s after sending. New API keys take up to ~1 minute to become active.
  - API Gateway (TLS) → Lambda authorizer (API key → tenant, from an `obs-tenants` table) → ingest Lambda → the tenant's own Firehose stream per signal → `_incoming/tenant=<T>/<signal>/`.
  - The tenant comes only from the authorizer; client-sent `obs.*` attributes are stripped.
  - Durability: the client gets 200 only once Firehose has stored the records; otherwise 503 and the SDK retries.
  - Per-tenant throttling and quotas via API Gateway usage plans.
  - No servers: nothing runs or costs while idle. (A first version with an OpenTelemetry Collector fleet behind a load balancer was built and tested locally, then replaced before deployment: it cost ~$75/month idle, and every job it did has a serverless equivalent.)
  - One Firehose stream per tenant per signal rather than one shared stream with dynamic partitioning: no partitioning fees ($0.020/GB, JQ hours, per-object charges), no shared 500-active-partition limit, per-tenant throughput limits, and a 30 s flush. Firehose bills each record as at least 5 KB, so tiny requests cost more per byte; T6 measures it.
- **T3. Fast lane for recent data (freshness).** ✅ Deployed and tested on AWS: records findable 35 s after sending (target 60 s), exact row count from raw files, trace ID found via the raw file's bloom filter, and after compaction the same 500 rows come from Parquet only, with the fast-lane records retired.
  - An S3 event (via EventBridge) on each new raw file triggers `obs-recent-indexer`, which indexes it as `kind=raw` entries: one per (service, event hour), with time range, row count and bloom filter. It uses compaction's own parsing code, so both see the same rows.
  - Lookups return raw files alongside compacted Parquet, marked by `kind`, so the query engine (Phase 4) reads both.
  - Handover: each raw file belongs to its arrival hour's compaction plan. While the plan is `planned`, raw entries are visible and the new Parquet entries hidden; one write marks it `committed`, which flips both; cleanup then deletes the raw entries and files. A search never counts a record twice or misses it, whatever step a crash interrupts.
- **T4. Traces and metrics compaction.** ✅ Deployed and tested on AWS: 300 spans and 200 metric points (gauge, sum, histogram) sent through the endpoint; findable in 35 s (spans) and 64 s (metrics: Firehose delivered that file ~30 s later than the spans file sent at the same moment; the indexer itself takes ~1 s); trace ID lookups hit the right file before and after compaction; exact counts after compaction; Athena returns the values sent. Spans and metric data points flattened to Parquet, with the same index, bloom filters (trace IDs for spans) and lookups as logs.
  - `obs.traces`: one row per span at its start time, events and links nested. `obs.metrics`: one row per data point; all five OTLP metric types in one table, `metric_type` saying which columns are set, sorted by metric then time.
  - Same fast lane, handover, chunking and crash safety as logs: one code path, parameterised by signal.
  - Not yet: metric exemplars (they would link metrics to traces); spans are placed by start time, so a lookup finds a long span by when it started.
- **T5. Tenant operations.** ✅ Deployed and tested on AWS: create (3 streams, working key); rotation (new key works, old key works through its grace then refused ~40 s after); usage exact per signal (100 logs, 50 spans, 20 points; bytes counted); delete (key refused in ~70-80 s, streams gone, every raw file, Parquet file and index entry purged in the first pass, lookups empty, a tenant whose id starts with the deleted id untouched, usage kept, "deleted" after a clean second pass). Onboarding (create tenant, issue and rotate API keys), per-tenant usage metering (bytes and records ingested, stored, scanned), and full tenant deletion (data, index entries and keys).
  - One control-plane Lambda, `obs-tenant-admin`: create, rotate (old keys work for a grace period), revoke, status, usage, list, delete. Serverless; a 15-minute sweep expires rotated keys and advances deletions.
  - Metering: each compacted chunk writes one usage record (records, raw bytes, stored bytes) before its commit, keyed by the chunk, so re-runs never double count. Bytes scanned come with the query engine (Phase 4).
  - Deletion: keys refused, streams deleted, then purge passes 20 minutes apart (longer than a worker lease) until one finds nothing, so in-flight compaction can't leave data behind. Usage records are kept.
- **T6. Scale, fault and soak tests** against the targets above.
  - Tooling (T6.1): serverless load generator (realistic OTLP protobuf through the public endpoint, 100 Zipf-sized tenants), a freshness prober, and a report with cost per GB. Steps run 45 min each; estimated platform cost ~$2 (1 GB/h), ~$3 (10 GB/h), ~$7 (50 GB/h), plus compaction.
  - Ramp load; inject faults under load (kill collector tasks, force S3 and DynamoDB throttling, lose an AZ); run for several days at steady load.
  - Freshness under load: a new stream's Firehose delivery sometimes takes ~60 s (seen once in T4); measure the distribution across many streams.
  - Fixed (T6 fixes A/B, before load): blooms over 1 KB moved from index items to S3; parallel lookups; sharded per-day ID filters built by an hourly sealer, trusted only while no chunk was added after sealing, so an ID lookup across 30 days reads ~30 small ranges. Dispatcher lists tenants in parallel. Fast-lane bloom files no longer leak.
  - Verified on AWS after the fixes: T3/T4 end-to-end tests pass; day filters (infra/phaseT6-dayfilter-test.sh): known ID found via the day filter, unknown ID rules the day out with no per-file bloom reads (173 ms), late data found via per-file blooms until re-sealed.
  - Found by the load-test smoke run: gzip-encoded requests (what OTLP SDKs send by default) were rejected with 400, because API Gateway had already decompressed the body but kept Content-Encoding: gzip. Ingest now checks the gzip magic bytes.
  - Smoke run after the gzip fix (5 tenants, 0.05 GB/h, 8 min): 47/47 requests accepted, 0 retries; freshness p50 34 s, max 36 s (9 probes); lookups p50 170-290 ms. At trickle volume cost is $0.22/GB, dominated by fixed per-record and per-file charges; the real steps measure it at volume. Even this trickle used 6 concurrent indexers and 5 ingest functions.
  - Found in preflight: the account's Lambda concurrency limit is 10 (new-account default). Raised limit requested; a production account needs 1,000+.
  - The dispatcher lists every tenant's raw folders each run; check it stays within its timeout at 100+ tenants.
- **Also:** configurable bloom attributes cover common request-ID names by default (`request.id`, `http.request_id`, `request_id`, `x-request-id`).

## Phase 4: Single-worker query path

**Build**
- A Lambda with DuckDB embedded that:
  1. calls the Phase 3 lookup,
  2. reads exactly those S3 objects through DuckDB's `httpfs` extension,
  3. runs the query and returns results.
- No parallelism yet.

**Test on AWS**
- Run "count errors by endpoint, last 24 hours" end to end. Record Lambda duration. This is the baseline for Phase 5.
- Run the same query in Athena on the same data and check the results match, before any speed work.
- Record cold-start cost from the `Init Duration` field in CloudWatch Logs. This informs whether provisioned concurrency is worth paying for later.

## Phase 5: Parallel fan-out

**Build**
- A Step Functions Map state that:
  - splits the Phase 3 file list into N chunks (balanced by `size_bytes`, not file count),
  - runs N Phase-4 workers at the same time,
  - merges their partial results in a final step.
- The coordinator writes `{files_done, files_total}` to a DynamoDB progress item as chunks finish. This drives the UI's scan-progress indicator.

**Test on AWS**
- Re-run the Phase 4 query with parallelism and compare wall-clock time.
- Sweep 4, 8, 16 and 32 workers on a large query. Plot latency against worker count with CloudWatch Logs Insights to find where returns flatten out.
- Search worker logs for S3 `503 SlowDown` errors at high concurrency. If they appear, add jittered backoff or spread reads across more prefixes.
- **Cost check:** sum the `Billed Duration` from each worker's REPORT log line (Logs Insights) and compare with Phase 4's single worker. Total compute-seconds should be about the same. Cost Explorer is too coarse to see a single experiment.

## Phase 6: Hot path and alerting

**Build**
- A small EC2 instance running single-node VictoriaMetrics, fed by the collector's `prometheusremotewrite` exporter. This path skips S3 entirely.
- vmalert evaluates the alerting rules and sends to Alertmanager.
- `-retentionPeriod` is set to a short window (e.g. 24h). Keeping this tier small is what keeps it cheap.
- This track doesn't depend on Phases 1–5 and can be built in parallel.

**Test on AWS**
- Push a synthetic metric spike and measure time-to-alert. The target is **under about 30 seconds**, set by the rule evaluation interval. The cold path takes minutes, and this test confirms the two really do behave differently.
- Confirm old data is dropped at the retention boundary.
- Reboot the instance, then simulate an AZ failure. Confirm alerting recovers.
- Add a daily EBS snapshot schedule. If alerting uptime matters, add a warm standby in a second AZ.

## Phase 7: Caching, pre-aggregation, cost estimates

**Build**
- **Result cache:**
  - DynamoDB table keyed on `hash(normalized query + time range)`, with TTL.
  - Results under 400 KB are stored inline. Larger ones go to `_results/` in S3 and the item holds a pointer.
  - It sits in front of the Phase 5 coordinator.
  - Consider ElastiCache Serverless (Valkey, the lower-minimum engine) only if cache read latency turns out to matter.
- **Cost estimate:** before a query runs, sum `row_count` and `size_bytes` for the files the index selects. Weight by `storage_class`, since Instant Retrieval reads carry a retrieval fee. Return the estimate to the UI.
- **Pre-aggregation:** pick the 3–5 most common dashboard queries and have the compaction worker write their rollups alongside each partition.

**Test on AWS**
- Run a popular query twice. On the second call, the Lambda invocation count in CloudWatch should stay flat.
- Run a very broad query and compare the estimate with the actual bytes scanned and duration. Tune it until the error is within an agreed margin (e.g. ±25%). A badly calibrated estimate is worse than none.

## Instrumentation (later phase)

The collector only receives OTLP (gRPC 4317, HTTP 4318); it does not scrape or pull anything. Real services will send telemetry with the **OpenTelemetry SDK**, pointed at the collector. Until then the only source is the `telemetrygen` load generator.

When this is picked up:
- Give the collector a stable address (an internal load balancer or Cloud Map name) instead of the task's private IP.
- Decide how services outside the platform VPC reach it (VPC peering or PrivateLink), since the security group admits only VPC traffic today.
- Extend compaction to traces and metrics before sending them, or raw files will pile up in `_incoming/traces/` and `_incoming/metrics/`.

## Data freshness

A cold-path query only sees data that has been compacted and indexed. Worst-case lag is about **one hour of partition + 10 minutes of grace + up to 15 minutes until the next dispatcher run**, so roughly 25 to 85 minutes.

- **v1 accepts this lag.** Real-time visibility comes from the hot path (Phase 6).
- If you later need to search recent logs and traces, the query path can also read the not-yet-compacted `_incoming/` hours. The catch: a file that has been compacted but not yet deleted would be counted twice. So only read `_incoming/` hours that have no index entries yet, or skip inputs whose `batch_id` is already in the index.

## Query frontend: build vs. Grafana

A custom React UI works fine behind API Gateway + Lambda, in front of the Phase 5 coordinator. The real cost is rebuilding what Grafana gives you for free: time-series charts, log search, trace waterfalls, dashboard sharing and alerting UI.

**Split:**
- **Custom UI** for what's specific to this platform:
  - the cost estimate shown before a query runs (Phase 7),
  - the scan-progress indicator driven by the Phase 5 progress item,
  - a trace waterfall.
- **Grafana** for generic time-series panels, through a small datasource plugin that calls the same query API.
- **Alerting UI:** the Grafana Alertmanager datasource pointed at the Phase 6 instance.

## Cost and scale guardrails

- Tag every resource `phase=N` and set a Budgets alert for each phase. A runaway fan-out in Phase 5 should be caught before it shows up in later bills.
- Line items to watch in Cost Explorer as each phase goes live:
  - Lambda cost at high parallelism (Phase 5)
  - DynamoDB reads from inefficient index queries (Phases 3–5)
  - S3 GET request counts (Phases 4–5)
  - Instant Retrieval fees once data passes 180 days
- Athena stays the baseline throughout. At every phase, compare what the custom pipeline cost for a query with what the same query would have cost as raw Athena. That's the evidence each layer is earning its complexity.
