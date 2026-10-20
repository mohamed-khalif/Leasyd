# Performance plan: slow pages, result cache, warm compute

Agreed 2026-10-20. Background and the baseline: `docs/bench/README.md` and the architecture doc
("Leasyd architecture and the 20 Oct page benchmark"). Flags: `docs/perf-flags.md`.

## Ground rules

- Branch `claude/code-identification-1dxuoa`; one commit per phase, with its benchmark files.
- The query functions serve every account, so each change is behind a **per-account flag**: an
  environment variable listing the accounts it is on for (`leasyd-demo` while testing; `*` = all).
  Default at merge: empty (off). Changing the list needs no code deploy.
- Claude writes templates and deploy scripts; the owner creates each change set, shares its summary
  and executes it. Owner sign-off before any IAM, new table or provisioned-concurrency change.
- The public query API (`/v1/app/query`, `/v1/query` bodies and answers, the 20 s wait and 202 jobs) does
  not change.
- **Correctness gate** per phase: every captured page question, old path vs new path, on a closed range
  (the previous full UTC day). Counts and integer sums equal; floating-point sums within 1e-9 relative;
  percentiles within the JSON path's bucket error (2.5%). A failed diff stops the phase.
- No change to retention, compaction output, or index items without the owner's yes first.
- Stop and ask if a phase adds more than $50 a month beyond what is listed here.
- Numbers come from benchmark files. Best-of-2 differing by more than 20%: rerun up to 5 times, report
  the median and range.

## Phase 0: baseline and instrumentation

1. Re-run `infra/bench/bench.py` unchanged (14 pages x 1 h/6 h/24 h, twice), with file counts per
   question: `docs/bench/2026-10-20-phase0.*`.
2. Per-question timing: workers report time, memory and whether their container was new; the
   coordinator logs one `{"question": …}` line per question (path, files, bytes, workers, cold
   containers, lookup/scan/merge ms, Lambda GB-seconds, cache). Queries: `infra/bench/questions.insights`.
3. `bench.py`: `--cold` (needs `lambda:UpdateFunctionConfiguration` on the three query functions, or
   the owner runs it), `--no-cache`, timing and GB-seconds in the JSON output; text format unchanged.

## Phase 1: SQL fan-out

1. Classify every SQL question the app sends (`docs/bench/sql-classification.md`): page, tables, and
   (a) decomposable aggregate, (b) needs trace locality (service map edges), (c) user SQL (SQL page).
2. (a): turn into JSON questions in the web app wherever the JSON shape can say it. The rest become
   **named server-side queries**, each with a hand-written per-slice query and merge query, run with the
   JSON path's slicing and range reads. No generic SQL rewriter.
3. (b) service map: compaction computes each closed hour's service-to-service edges into a small rollup
   (a new compaction output: owner approval first); the open hour is computed at query time.
4. (c) the SQL page keeps today's single-worker path and its 4,000 file / 1 GB limits.
5. **Stop for approval** of steps 1-3 before writing fan-out code.

Acceptance: Web, Kubernetes, GPUs, Lambda, Services at 24 h <= 3.5 s warm; no page more than 10% slower.

## Phase 2: server-side result cache (in obs-query)

- In the coordinator: it sees the normalised question and the index's file list, and also serves
  PromQL, alerts and the AI SRE (and is what the benchmark calls).
- Per closed hour, mergeable partials keyed by account (from the authorizer, never the body), question
  without its time range, hour and engine version; valid while the hour's file-set fingerprint is
  unchanged. Open hour: 30 s. Refresh bypasses it. Account delete removes the account's entries.
- Cached per hour: counts, sums, min/max, percentile buckets. Not cached until handled: counter
  increases and counter buckets (a rise cannot be split at an hour edge), PromQL windows that cross an
  hour edge, searches with a limit.
- `docs/caching-decision.md` gets a section: the 17 Oct "no cache" covered the browser only.

Acceptance: second visit of any page at 24 h <= 0.5 s warm **within 30 s**; first visit not slower than
Phase 1; never stale after a forced compaction.

## Phase 3: warm compute

- Provisioned concurrency **3 + 3** (`obs-query-api`, `obs-query`), business hours Mon-Fri 07:00-19:00
  Europe/London by scheduled scaling; an alias, and API Gateway (ingest stack, T2) pointed at it: two
  change sets. Real cost from AWS pricing written in `docs/perf-flags.md` (estimate about $45 a month).
- A worker pool only if the timing shows workers are over 25% of the first-load penalty.
- A Parquet footer cache in worker memory and `/tmp` (key + ETag). DuckDB already loads at start-up.

Acceptance: Home's first load within 0.5 s of its second.

## Phase 5, then Phase 4

- Phase 5, fewer questions per page: merge overlapping questions on Home (20) and the built-in
  dashboard (15) without changing what is shown; `docs/bench/question-merge.md`. Home <= 10 questions, < 2 s.
- Phase 4, fewer small files in the open hour: merge fast-lane files every 5 minutes (a second
  compaction level; owner approval first, it changes index entries). 1 h questions read tens of files.

## Final

`docs/bench/<day>-comparison.md`: page x range x every phase, seconds and % change, file counts and
Lambda GB-seconds per page; cost section; unmet targets with measured numbers; open decisions.
