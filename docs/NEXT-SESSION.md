# Prompt for the next session: fast-lane freshness at 50 GB/h

**Status: implemented in the original session (fast parse in the fast lane, stage timing,
per-tenant Firehose buffer); kept for reference.**

Copy everything below the line into a new session.

---

Repo mohamed-khalif/Leasyd, branch `claude/code-identification-1dxuoa` (develop and push only there).
Read first, in this order: `docs/REPORT-2026-09-28.md` (goal, architecture, every benchmark),
`docs/STATUS.md`, then `services/compaction/compact.py` and `recent_indexer` in
`services/compaction/handler.py`. I deploy in AWS CloudShell (`cd ~/Leasyd && git pull && <deploy
script>`); you run the tests from this session with the AWS CLI (user `obs-deployer`, us-east-1,
account 199301651524). Explain results to me in plain end-user terms, briefly. Do not add
workarounds on top of workarounds: fix the cause with the smallest change and keep the code simple.

## Problem

At 50 GB/h, freshness (record sent -> findable) is p99 74 s; the target is p99 < 60 s (step
`s3e` in the report). Cause, measured: the fast lane (`recent_indexer`) and compaction share one
parser, `compact.compact()` -> `load_rows()` -> `_ROWS_SQL[signal]`. Commit 7b2f5d7 reverted the
attribute-map CASE shortcut in `_ATTR_MAP` (compact.py, ~line 96) because it stops DuckDB spilling:
compaction chunks of ~2M records then fail out of memory on every retry. The spill-safe expression
is ~1.8x slower, so the fast lane's p99 per raw file went from 13 s to 21 s.

Measured with `infra/t6/parse-bench.py` (Lambda limits: DuckDB memory_limit 1804 MB, 2 threads):

| Input | CASE shortcut | spill-safe (current) |
|---|---|---|
| 1 raw file, 15 MB gzip, 162k records (fast lane) | 14.0 s, RSS 678 MB | 25.7 s, RSS 705 MB |
| 4 files, 59 MB gzip, 643k records | OK, 36 s, RSS 1,872 MB | — |
| 8 files, 123 MB, 1.33M records | OOM | OK, 170 s |
| 12 files, 188 MB, 2.03M records (a real compaction chunk) | OOM | OK, 265 s |

The fast lane only ever parses one raw file. Firehose caps a file at 64 MB of gzipped records
(buffer 30 s / 64 MB, `services/tenants/admin.py`); observed files for the largest tenant are
15-17 MB gzip. So the CASE form is memory-safe for the fast lane and not for compaction.

## Task

1. In `compact.py`, make the attribute-map expression a choice of the caller, one code path:
   e.g. `compact(..., spill_safe=True)` passed through `load_rows` to the `_logs_sql` /
   `_traces_sql` / `_metrics_sql` builders (which currently read the module-level `_ATTR_MAP` and
   `_RES_ATTRS`; `_RES_ATTRS` is built from `_ATTR_MAP` at import, so it must follow the choice
   too, as must the span-event attribute maps in `_traces_sql`). Restore the CASE form from
   ac003b8 (`git show ac003b8 -- services/compaction/compact.py`) as the non-default option.
   Keep the comment explaining why compaction must use the spill-safe form.
2. `recent_indexer` passes `spill_safe=False`; the compaction worker keeps the default (`True`).
3. Tests (`services/compaction`, `python3 -m pytest -q`, currently 124 pass): add one that compacts
   the same inputs with both forms and asserts identical Parquet rows (every column) and identical
   blooms, with inputs that include duplicate attribute keys, empty/missing attribute lists,
   resource attributes with duplicates, span events with attributes, and malformed values (see
   `test_attribute_types_and_duplicate_keys` and `test_malformed_values_do_not_fail_the_chunk`
   in `test_compact.py`, and the trace/metric fixtures in `test_signals.py`, for inputs to reuse).
4. Before pushing, measure with `infra/t6/parse-bench.py` (it downloads the real 2M-record chunk
   from `s3://obs-data-199301651524-us-east-1/_bench/chunk-2m/` on first use): fast-lane form on
   1 file and on 4 files (59 MB, near the 64 MB Firehose cap) must pass with RSS well under 3008 MB;
   compaction form on 12 files must still pass. Add a flag to the script to choose the form rather
   than monkeypatching. If the 4-file case does not fit, stop and tell me before adding any
   size-based branching.
5. Commit, push, and ask me to deploy: `cd ~/Leasyd && git pull && infra/deploy-phase2.sh`.
   Confirm the deploy took: `aws lambda get-function-configuration --function-name
   obs-recent-indexer --query LastModified`.

## Verify on AWS (one clean run; understand what each number measures before reporting it)

- Load-test keys are not in a new container: run `python3 infra/t6/loadtest.py tenants create`
  first (existing `t6-*` tenants get a fresh key via rotate; wait ~2 min for keys to activate).
- Before starting, check nothing is stuck: `aws dynamodb scan --table-name obs-index
  --filter-expression "contains(pk, :p)" --expression-attribute-values '{":p":{"S":"#_plan#"}}'
  --select COUNT` should be 0 or only plans for the hour just closed.
- `python3 infra/t6/loadtest.py run --gbph 50 --minutes 20 --step s3f --yes`, then 4 min after it
  ends `python3 infra/t6/loadtest.py report --step s3f`. 13 min into the run:
  `python3 infra/phase4-test.py`.
- ~25 min after the run's last hour ends, confirm compaction: no plans left for that hour, and
  `aws logs filter-log-events --log-group-name /aws/lambda/obs-compaction-worker --filter-pattern
  '?OutOfMemory ?"Task timed out" ?"Runtime exited" ?errorType'` returns nothing for the window.

Acceptance, all on the same run: freshness p99 < 60 s and 0 records not found; obs-recent-indexer
p99 duration <= ~13 s and 0 errors; all 114k+ requests 200; `phase4-test.py` all PASS (1 day < 5 s,
ID lookup across 30 days < 3 s, same as Athena); compaction 0 OOM/timeouts; cost ~$0.065/GB.
Record the results in `docs/observability-plan.md` (T6 section), `docs/STATUS.md` and a new row in
`docs/REPORT-2026-09-28.md` section 4, then commit and push.

## Not in scope

Fault tests, soak, tenant cleanup, Phase 0 lock-down, merging to main (listed in `docs/STATUS.md`
as next steps). Do not change Firehose buffering, Lambda memory (3008 MB is this account's
maximum) or compaction chunk sizes as part of this task.
