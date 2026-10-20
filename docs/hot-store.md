# Hot store

Recent data answered from memory, so pages show data seconds old without waiting for files.

## How data flows

1. `obs-ingest` puts each batch on the tenant's Firehose as before. For accounts in `HOT_ACCOUNTS` it also
   puts the same record on the Kinesis stream `obs-hot` (`"<tenant> <signal>\n" + record`, partition key the
   tenant). The put has a 1 s connect / 2 s read limit and one attempt; a failure is logged
   (`hot_put_failed`) and never fails the request.
2. One Fargate task (`services/hot/server.py`, stack `obs-phaseH1`) reads every shard and loads the rows into
   in-memory DuckDB tables (`services/compaction/hotstore.py`), the same columns the files have. It keeps
   `HOT_KEEP_S` (2 hours) and drops older rows every minute. On start it rereads the stream from 2 hours ago
   (the stream keeps 24 hours), so it holds no state of its own.
3. `obs-query` (in the stack's private subnets) asks `GET /status` (at most every 5 s). For a flagged
   account whose signals the store covers and which is less than 15 s behind the stream, it cuts the range at
   `cut` = the later of (store coverage, now - 2 h) + 2 min, rounded up to a second:
   - before `cut`: the files, as before;
   - from `cut`: the store (`POST /worker` for JSON questions and PromQL, `POST /scan` for the SQL fan-out);
   - the two answers merge like two workers' (counters stitch across the cut).
4. If the store fails, times out (20 s) or answers with an error, the whole question is asked of the files
   (`hot_failed` in the log). Answers say where the cut was in `stats.hot_from`.

Summary tables (`svc_1m`, `edge_1m`, `rum`) are built from the store's rows on the fly with the same SQL
compaction uses.

## Network

- VPC `10.40.0.0/16`: two public subnets (the task, with a public IP: no NAT) and two private subnets
  (`obs-query`).
- `obs-query` reaches S3 and DynamoDB through gateway endpoints and Lambda and STS through interface
  endpoints; it has no internet.
- The task accepts port 8080 from `obs-query`'s security group only. Its address: `hot.obs.internal`
  (Cloud Map).

## Deploy

```
infra/deploy-phase0.sh                                        # boundary: VPC Lambda, obs-hot stream
infra/deploy-phaseH1.sh                                       # VPC, stream, image, task
infra/deploy-phase4.sh --parameter-overrides HotAccounts=leasyd-demo
infra/deploy-phaseT2.sh HotStream=obs-hot HotAccounts=leasyd-demo
```

Off: `HotAccounts=""` on obs-phase4 (queries) and obs-phaseT2 (copies). Stop the task:
`infra/deploy-phaseH1.sh --parameter-overrides DesiredCount=0`.

## Check it

- `/ecs/obs-hot` logs `{"hot_store": "serving"}`; the ECS service shows the task healthy.
- `obs-query` answers carry `stats.hot_from` for flagged accounts; `hot_failed` lines mean fallbacks.
- Same question with and without the account in `HotAccounts`: same answer.
