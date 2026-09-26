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
| Agents/collectors | OpenTelemetry Collector on ECS Fargate. One agent for all three signal types. |
| Cold-path writer | The collector's S3 exporter, writing OTLP JSON to `_incoming/` |
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
  - Invokes one worker per (signal, arrival hour).
  - Publishes a CloudWatch metric for the age of the oldest `_incoming/` object, with an alarm if it goes over 3 hours.
- **Worker Lambda**, per partition:
  1. List the input files and compute `batch_id = hash(sorted input keys)`.
  2. Read the inputs, split rows by service and by event hour, convert to Parquet, sort by timestamp, and write files of 128–512 MB named `part-<batch_id>-NNN.parquet` under each `{signal}/dt=/hour=/service=/`. The same inputs always produce the same names, so a re-run overwrites instead of duplicating.
  3. Write the index entries (Phase 3). Because the keys are also derived from `batch_id`, re-writing them is harmless.
  4. Delete the input files.
- Data that arrives after its hour was compacted gets picked up on the next run. Its input set is different, so it gets a new `batch_id` and an additional file.
- Files never cross an hour boundary, so every file covers at most one hour. Phase 3's range lookup depends on this.
- If one partition is too big for a single run (15-minute timeout, 10 GB memory), the worker splits the input list into chunks. The chunk number becomes part of `batch_id`.

**Test on AWS**
- Run the Phase 1 Athena query against the compacted Parquet. Compare bytes scanned and duration with the raw-JSON baseline. This is the billed-in-dollars case for compaction.
- Kill a worker at each step boundary (after step 2, after step 3). Re-run it. Confirm row counts match and there are no duplicate files or index entries.
- Use S3 Storage Lens to confirm average object size goes up and object count goes down.

## Phase 3: Metadata index

**Build**
- DynamoDB table, on-demand billing:
  - **Partition key:** `signal#service`
  - **Sort key:** `min_ts#file_id`
  - **Attributes:**
    - `max_ts`, `file_path`, `row_count`, `size_bytes`
    - `storage_class`: set when lifecycle rules move the file, for cost estimates
    - `bloom`: the bloom filter for high-cardinality fields (trace_id, request_id, etc.). Stored inline if under about 300 KB. Otherwise it goes to S3 and the item holds `bloom_s3_key`.
- The Phase 2 worker fills the index in the same pass, since it already reads every row.
- **Range lookup:** for `[t0, t1]`, query `min_ts BETWEEN (t0 − 1h) AND t1`, then filter `max_ts ≥ t0`. The one-hour lookback works because no file spans more than one hour.

**Test on AWS**
- Write a small Lambda that takes a time range and a service and returns file paths. Compare its result with a naive S3 listing of the same range: the counts should show real pruning.
- Build a test file that crosses an hour boundary within its own hour (e.g. 10:40–10:59). Query from 10:50 and confirm the file is returned.
- Look up a known trace_id and confirm the bloom filters rule out most files.
- Watch consumed capacity in CloudWatch and confirm cost is near zero at idle.

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
