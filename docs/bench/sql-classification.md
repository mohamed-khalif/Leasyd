# Phase 1: the app's SQL, classified, and the fan-out design

Status: **design, waiting for approval** (docs/perf-plan.md, Phase 1 step 5). Source: the captured
page queries (`services/web/scripts/perf/capture-queries.cjs`, 2026-10-20), 22 distinct SQL texts
(time-bucket variants of the same query counted once).

## 1. Classification

| Page | Questions | Table | What the SQL does | Class |
|---|---|---|---|---|
| Services | 1 | spans | per service: requests, errors, avg duration, `any_value` of 17 resource attributes | (a) |
| Services | 1 | spans x spans | edges: self-join child to parent on `trace_id`, `parent_span_id` | (b) |
| Web | 8 | logs | RUM: `count FILTER`, `count(DISTINCT session)`, `quantile_cont` p75, `max(ts)`, `any_value`, ORDER BY + LIMIT; all on `telemetry.sdk.name = 'leasyd-rum'`, 7 of 8 also `service = 'shop-web'` | (a) but not exactly decomposable: distinct counts and exact percentiles need the raw values |
| Lambda | 1 | spans | per service: count, errors, avg duration, `any_value` | (a) |
| Lambda | 1 | metrics | per service: `sum(sum) FILTER`, `metric_name IN (3 names)` | (a) |
| Kubernetes | 1 | metrics + logs | discovery: 3 counts, `UNION ALL` | (a) |
| Kubernetes | 1 | metrics | two levels: per (bucket, namespace, pod) avg, then sum per (bucket, namespace) | (a), two-level |
| Kubernetes | 1 | metrics | workloads: `arg_max(value, ts) FILTER` | (a) |
| Kubernetes | 1 | logs | events: count, min/max ts, `arg_max(body, ts)`, ORDER BY + LIMIT 500 | (a) |
| Kubernetes | 1 | metrics | nodes: 3 CTEs, LEFT JOINs, subquery `max(ts) - 3 min` | not decomposable |
| Kubernetes | 1 | metrics | pods: 5 CTEs, FULL OUTER JOIN, `string_agg DISTINCT`, subquery on `max(last_seen)` | not decomposable |
| GPUs | 2 | metrics | two levels: per (GPU, minute) then per minute or per GPU, `arg_max`, `count FILTER` | (a), two-level |
| GPUs | 1 | metrics | health: `list(DISTINCT …)`, max - min, HAVING | (a) |
| SQL page | any | any | the customer's own SQL | (c) |

**What this shows.** Splitting these queries into "per-slice part + merge part" (the plan's earlier
idea, hand-written per query) would be exact for only about half of them. Distinct counts, exact
percentiles, the two-level CTEs, the joins and the "latest 3 minutes" subqueries all need either
approximations (which would change what pages show) or all the rows in one place. Turning them into
JSON questions doesn't work either: the JSON shape has no per-aggregate `FILTER`, `any_value`,
`arg_max` or distinct count, so one SQL question would become 3-10 JSON questions and pages would
send more, not fewer.

But every one of them reads **few columns and few rows** of what it downloads today:

| Page | Columns used (of 20-30) | Rows used |
|---|---|---|
| Web | `attributes`, `resource_attributes`, `service`, `ts`, `trace_id` | RUM logs of one service only |
| Kubernetes, GPUs, Lambda (metrics) | `metric_name`, `value`, `ts`, `attributes`, `resource_attributes`, `service`, `sum`, `count` | 1-10 named metrics; metrics files are sorted by `metric_name`, so whole row groups are skipped |
| Services | 6 columns (nodes); `trace_id`, `span_id`, `parent_span_id`, `service`, `status_code` (edges) | all spans |

## 2. Design (proposed): fan out the scan, keep the SQL unchanged

The page's SQL runs **unchanged**, in one place, but over a much smaller input that many workers
prepare in parallel. Results are exact by construction: the final query sees every row it would
have seen, only without the columns and rows it never touches.

1. **Analyse** (coordinator). `json_serialize_sql` (DuckDB's own parser) gives, for each table the
   query reads (`logs`, `spans`, `metrics`), every reference to it and the columns used. Tested on all
   22 captured queries: all parse. For each table:
   - **columns** = the columns referenced, plus `ts` (the time filter), intersected with the table's
     real columns;
   - **row filter** = for each reference, the AND-ed conditions in its own `WHERE` that use only that
     table's columns and simple forms (`col = 'x'`, `col IN (…)`, `map['k'] = 'x'`, `IS NOT NULL`);
     the table's filter is the OR of its references' filters. If any reference has no such condition,
     the table gets no row filter (all rows). This only ever keeps a superset of the rows needed.
   - `service = 'x'` / `service IN (…)` conditions also go to the index lookup, which skips other
     services' files entirely (fast-lane files are per service).
   - Anything it cannot analyse (parse error, a table function, a reference it can't place) uses
     today's single-worker path unchanged.
2. **Scan** (workers, in parallel). Files are sliced with the JSON path's cost model. Each worker
   reads its slice **in place** (range reads: only those columns, and for metrics only the row groups
   whose `metric_name` range matches), applies the row filter, and returns the reduced rows as
   compressed Arrow. A worker whose reduced rows exceed its response limit (6 MB) makes the
   question fall back to the single-worker path.
3. **Run** (coordinator). Loads the reduced rows as the `logs` / `spans` / `metrics` views (same time
   window as today) and runs the original SQL with the same lockdown settings as the SQL worker.
4. **SQL page** (class c): the same analysis applies; it is behind its own flag, off at first.
5. **Service map edges** (class b): the scan fan-out cuts them to 5 columns of every span. If the map
   is still over the 3.5 s target at 24 h after this, the follow-up is the compaction-time rollup of
   each closed hour's edges (a new compaction output: separate approval).

**Flags:** `SQL_SCAN_FANOUT_ACCOUNTS` (app pages), `SQL_SCAN_FANOUT_USER_ACCOUNTS` (the SQL page), both
on `obs-query`, empty at merge.

**Fits Phase 2.** A reduced scan of a closed hour (table, columns, row filter, hour, file
fingerprint) is exactly what the result cache can keep: a 24-hour page then scans only the open
hour and runs its SQL over cached inputs.

## 3. Also proposed: a first, smaller step

Before the fan-out, make today's single SQL worker read Parquet **in place** with the same column
list (flag `SQL_IN_PLACE_ACCOUNTS`), as JSON questions already do. It needs no analysis of row
filters and no merging. On compacted hours (22-34 large files sorted by `metric_name`) it should
cut most of the bytes; on uncompacted hours (~1,000 tiny files) range reads can be slower than
today's 64-thread downloads, which the benchmark will show. It ships first, measured on its own.

## 4. Correctness gate for Phase 1

All 22 captured SQL texts, previous full UTC day, old path vs new path, results compared row by row
(sorted when the query has no ORDER BY): equal, floating-point values within 1e-9 relative.
Plus unit tests of the analysis: filters ORed across references, a reference without filter
removing the row filter, subqueries, CTE names not mistaken for tables, unparseable SQL falling back.

## 5. What I need from you

- Approve the **scan fan-out** (section 2) in place of hand-written per-query merge SQL, and the
  in-place first step (section 3).
- The service-map edge rollup stays a separate decision, only if the map misses its target.
