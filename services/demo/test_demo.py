import gzip
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))
sys.path.insert(0, os.path.join(HERE, "..", "compaction"))

import demo  # noqa: E402

NOON = 29_300_000 // 1440 * 1440 + 15 * 60 + 30  # 15:30 UTC: peak traffic, no incident (they start on the hour)
NIGHT = NOON - 12 * 60                          # 03:30 UTC: quietest
INCIDENT = NOON // demo.INCIDENT_EVERY * demo.INCIDENT_EVERY + 180 + 5   # 5 minutes into an incident


def spans(docs):
    return [s for rs in docs["traces"]["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]


def svc(rs):
    return next(a["value"]["stringValue"] for a in rs["resource"]["attributes"] if a["key"] == "service.name")


def test_same_minute_same_data_and_traffic_follows_the_day():
    assert json.dumps(demo.documents(NOON)) == json.dumps(demo.documents(NOON))
    roots = lambda m: sum(1 for s in spans(demo.documents(m)) if "parentSpanId" not in s)
    day, night = roots(NOON), roots(NIGHT)
    assert 100 <= day <= 135 and 35 <= night <= 50


def test_traces_are_connected_trees_and_logs_point_at_their_spans():
    docs = demo.documents(NOON)
    ss = spans(docs)
    by_trace = {}
    for s in ss:
        by_trace.setdefault(s["traceId"], []).append(s)
    for trace in by_trace.values():
        ids = {s["spanId"] for s in trace}
        assert sum(1 for s in trace if "parentSpanId" not in s) == 1
        assert all(s["parentSpanId"] in ids for s in trace if "parentSpanId" in s)
        assert all(int(s["startTimeUnixNano"]) < int(s["endTimeUnixNano"]) for s in trace)
    assert max(len(t) for t in by_trace.values()) >= 12           # a checkout crosses most services
    assert len({svc(rs) for rs in docs["traces"]["resourceSpans"]}) == len(demo.SERVICES)
    span_ids = {(s["traceId"], s["spanId"]) for s in ss}
    logs = [r for rl in docs["logs"]["resourceLogs"] for sl in rl["scopeLogs"] for r in sl["logRecords"]]
    assert logs and all((r["traceId"], r["spanId"]) in span_ids for r in logs)


def test_incidents_slow_shipping_and_fail_checkouts():
    def shipping(m):
        docs = demo.documents(m)
        quotes = [s for rs in docs["traces"]["resourceSpans"] if svc(rs) == "shipping"
                  for ss in rs["scopeSpans"] for s in ss["spans"] if s["name"] == "GetQuote"]
        slow = sorted((int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6 for s in quotes)[len(quotes) // 2]
        return slow, sum(1 for s in quotes if s["status"])
    calm, bad = shipping(NOON), shipping(INCIDENT)
    assert bad[0] > 5 * calm[0] and bad[1] > 0 and calm[1] == 0
    errors = [r for rl in demo.documents(INCIDENT)["logs"]["resourceLogs"] for sl in rl["scopeLogs"]
              for r in sl["logRecords"] if r["severityText"] == "ERROR"]
    assert any("timed out" in r["body"]["stringValue"] for r in errors)


def test_orders_counter_rises_and_resets_when_a_pod_restarts():
    totals = [demo._orders_total(m, 0, "card") for m in range(NOON, NOON + 400)]
    rises = [b[0] - a[0] for a, b in zip(totals, totals[1:])]
    restarts = [i for i, (a, b) in enumerate(zip(totals, totals[1:])) if b[1] != a[1]]
    assert len(restarts) == 1 and rises[restarts[0]] < 0            # one restart in 400 minutes: the value drops
    assert all(r >= 0 for i, r in enumerate(rises) if i not in restarts)


def test_every_signal_is_accepted_by_ingest_and_compacts(tmp_path):
    import duckdb
    import compact
    import ingest
    docs = demo.documents(NOON)
    for signal, doc in docs.items():
        body = gzip.compress(json.dumps(doc).encode())
        assert len(body) < 1_000_000                                 # one request per signal a minute
        ingest.parse(signal, gzip.decompress(body), "application/json")
        raw = tmp_path / f"{signal}.json.gz"
        raw.write_bytes(body)
        written = compact.compact(signal, [str(raw)], str(tmp_path / signal), "b1", "2025-09-16", "15")
        assert sum(w["rows"] for w in written) > 0
    kinds = duckdb.sql(f"SELECT DISTINCT metric_name, metric_type FROM '{tmp_path}/metrics/**/*.parquet' ORDER BY 1").fetchall()
    assert kinds == [("app.orders.placed", "sum"), ("app.payments.declined", "sum"),
                     ("db.client.connections.usage", "sum"), ("http.server.request.duration", "histogram"),
                     ("kafka.consumer.lag", "gauge"), ("process.cpu.utilization", "gauge"),
                     ("process.memory.usage", "sum")]
