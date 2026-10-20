# Summaries

Small tables made from the data as it arrives, so pages read them instead of every raw record.
Defined in `services/compaction/compact.py` (`SUMMARIES`); queried in SQL by these names.

| Table | One row per | Columns | Read by |
|---|---|---|---|
| `svc_1m` | minute, service, platform | requests (server or entry spans), errors, duration_ns_sum, service details | Services (map nodes), Lambda (function list) |
| `edge_1m` | minute, calling service, called service | calls, errors | Services (map links) |
| `rum` | browser event | ts, service, trace_id, body, severity_number, attributes, resource_attributes | Web |

**When they're made.** The fast lane writes them for each raw file as it lands (searchable within about a
minute), and compaction rewrites them for the whole hour, replacing the fast lane's (no double counting).
Hours compacted before summaries existed are built once by `infra/backfill_summaries.py`.

**Known differences from the raw data.**
- Service links: a call is counted when the caller's span is in the same raw file (fast lane) or the same
  compacted hour; one that crosses an hour boundary is missed (the raw query misses those at the edges of
  its time range too).
- Minute buckets: a time range starting mid-minute counts that whole minute or none of it.

Kept 30 days like the data (the retention sweeper lists the tables: `services/tenants/admin.py`
`SUMMARY_TABLES`; a test keeps the two lists equal). About 1% extra storage.
