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


def spans(docs, agents=False):
    """The shop's spans (or, agents=True, only the support assistant's AI agent spans)."""
    return [s for rs in docs["traces"]["resourceSpans"] if (svc(rs) == demo.agents.SERVICE) == agents
            for ss in rs["scopeSpans"] for s in ss["spans"]]


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
    assert {svc(rs) for rs in docs["traces"]["resourceSpans"]} == {*demo.SERVICES, demo.agents.SERVICE}
    span_ids = {(s["traceId"], s["spanId"]) for s in ss}
    logs = [r for rl in docs["logs"]["resourceLogs"] for sl in rl["scopeLogs"] for r in sl["logRecords"]
            if sl["scope"]["name"] != "leasyd-rum" and "k8seventsreceiver" not in sl["scope"]["name"]]
    assert logs and all((r["traceId"], r["spanId"]) in span_ids for r in logs)


def test_the_website_sends_real_user_events_linked_to_backend_traces():
    m = demo.Minute(NOON - NOON % demo.INCIDENT_EVERY + 2)       # during an incident: checkouts fail
    docs = demo.documents(m.minute)
    traces = {s["traceId"] for s in spans(docs)}
    web = [r for rl in docs["logs"]["resourceLogs"] for sl in rl["scopeLogs"] if sl["scope"]["name"] == "leasyd-rum"
           for r in sl["logRecords"]]
    kind = lambda r: next(a["value"]["stringValue"] for a in r["attributes"] if a["key"] == "rum.type")   # noqa: E731
    kinds = {kind(r) for r in web}
    assert {"page_view", "web_vital", "api_call"} <= kinds
    calls = [r for r in web if kind(r) == "api_call"]
    assert calls and all(r["traceId"] in traces for r in calls)    # a failed checkout opens its backend trace
    assert all(svc(rl) == "shop-web" for rl in docs["logs"]["resourceLogs"] if rl["scopeLogs"][0]["scope"]["name"] == "leasyd-rum")


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
    kinds = duckdb.sql(f"SELECT DISTINCT metric_name, metric_type FROM '{tmp_path}/metrics/**/*.parquet' "
                       "WHERE service NOT IN ('kubernetes', 'dcgm-exporter') ORDER BY 1").fetchall()
    assert kinds == [("app.orders.placed", "sum"), ("app.payments.declined", "sum"),
                     ("db.client.connections.usage", "sum"), ("http.server.request.duration", "histogram"),
                     ("kafka.consumer.lag", "gauge"), ("process.cpu.utilization", "gauge"),
                     ("process.memory.usage", "sum")]


def test_the_cluster_rides_along_and_compacts(tmp_path):
    """The Kubernetes cluster and GPUs (cluster.py): what Leasyd's collectors send, under the services
    kubernetes and dcgm-exporter, accepted by ingest and compacted like any data."""
    import duckdb
    import compact
    import ingest
    docs = demo.documents(NOON)
    for signal in ("metrics", "logs"):
        body = json.dumps(docs[signal]).encode()
        ingest.parse(signal, body, "application/json")
        raw = tmp_path / f"{signal}.json.gz"
        raw.write_bytes(gzip.compress(body))
        compact.compact(signal, [str(raw)], str(tmp_path / signal), "b1", "2025-09-16", "15")
    by = dict(duckdb.sql(f"SELECT service, count(DISTINCT metric_name) FROM '{tmp_path}/metrics/**/*.parquet' "
                         "WHERE service IN ('kubernetes', 'dcgm-exporter') GROUP BY 1").fetchall())
    assert by["dcgm-exporter"] >= 20 and by["kubernetes"] >= 25
    gpus = duckdb.sql(f"SELECT count(DISTINCT attributes['UUID']) FROM '{tmp_path}/metrics/**/*.parquet' "
                      "WHERE metric_name = 'DCGM_FI_DEV_GPU_UTIL'").fetchone()[0]
    assert gpus == 16
    events = duckdb.sql(f"SELECT count(*) FROM '{tmp_path}/logs/**/*.parquet' WHERE attributes['k8s.event.reason'] IS NOT NULL").fetchone()[0]
    assert events >= 1   # NOON is a multiple of 5 minutes: the stalled job's FailedScheduling


def test_the_support_assistant_sends_agent_sessions_with_their_failures(tmp_path):
    """agents.py: sessions over several minutes, one trace a turn, GenAI spans whose agent fields
    compact into columns, and over an hour each failure the Agents page looks for."""
    import duckdb
    import compact
    import ingest
    hour = range(NOON, NOON + 60)
    by_trace, sessions, failures = {}, {}, {"tool": set(), "llm": 0, "empty": 0, "loop": 0}
    for m in hour:
        ss = spans(demo.documents(m), agents=True)
        assert json.dumps(ss) == json.dumps(spans(demo.documents(m), agents=True))   # deterministic
        for s in ss:
            by_trace.setdefault(s["traceId"], []).append(s)
    for trace in by_trace.values():
        a = {s["spanId"]: {x["key"]: next(iter(x["value"].values())) for x in s["attributes"]} for s in trace}
        [root] = [s for s in trace if "parentSpanId" not in s]
        assert all(s["parentSpanId"] == root["spanId"] for s in trace if s is not root)
        sessions.setdefault(a[root["spanId"]]["gen_ai.conversation.id"], set()).add(root["traceId"])
        tools = [a[s["spanId"]]["gen_ai.tool.name"] for s in trace if a[s["spanId"]]["gen_ai.operation.name"] == "execute_tool"]
        failures["loop"] += tools.count("searchProducts") >= 5
        for s in trace:
            at = a[s["spanId"]]
            if s.get("status", {}).get("code") == 2:
                if at["gen_ai.operation.name"] == "execute_tool":
                    failures["tool"].add(at["gen_ai.tool.name"])
                else:
                    failures["llm"] += 1
            elif at["gen_ai.operation.name"] == "chat" and at["gen_ai.usage.output_tokens"] == "0":
                failures["empty"] += 1
    assert 40 <= len(sessions) <= 120 and max(len(t) for t in sessions.values()) >= 3   # conversations span turns
    assert failures["tool"] == {"initiateReturn", "checkInventory"}
    assert failures["llm"] >= 1 and failures["empty"] >= 1 and failures["loop"] >= 1
    body = json.dumps(demo.documents(NOON)["traces"]).encode()
    ingest.parse("traces", body, "application/json")
    raw = tmp_path / "t.json.gz"
    raw.write_bytes(gzip.compress(body))
    compact.compact("traces", [str(raw)], str(tmp_path / "t"), "b1", "2025-09-16", "15")
    got = duckdb.sql(f"SELECT count(DISTINCT session_id), sum(gen_ai_input_tokens) > 0, count(gen_ai_tool) > 0 "
                     f"FROM '{tmp_path}/t/**/*.parquet' WHERE service = 'support-assistant'").fetchone()
    assert got[0] >= 1 and got[1] and got[2]
