import droprules


def kv(k, v):
    return {"key": k, "value": {"stringValue": v} if isinstance(v, str) else {"intValue": str(v)}}


def logs_doc(*records, service="api"):
    return {"resourceLogs": [{"resource": {"attributes": [kv("service.name", service)]},
                              "scopeLogs": [{"logRecords": list(records)}]}]}


def spans_doc(*spans, service="api"):
    return {"resourceSpans": [{"resource": {"attributes": [kv("service.name", service)]},
                               "scopeSpans": [{"spans": list(spans)}]}]}


def rule(signal, *conds, keep=0, enabled=True):
    return {"id": "r", "name": "r", "signal": signal, "enabled": enabled, "keep_percent": keep,
            "conditions": [{"field": f, "op": o, "value": v} for f, o, v in conds]}


def test_debug_logs_dropped_and_counted():
    doc = logs_doc({"severityNumber": 5, "body": {"stringValue": "debug"}},
                   {"severityNumber": 9, "body": {"stringValue": "info"}},
                   {"severityNumber": 17, "body": {"stringValue": "boom"}})
    assert droprules.apply("logs", doc, [rule("logs", ("severity_number", "<", 9))]) == (3, 1)
    assert [r["severityNumber"] for r in doc["resourceLogs"][0]["scopeLogs"][0]["logRecords"]] == [9, 17]


def test_health_check_spans_of_one_service_only():
    span = lambda name, route: {"name": name, "traceId": "ab" * 16, "attributes": [kv("http.route", route)]}  # noqa: E731
    rules = [rule("traces", ("service", "=", "api"), ("attributes.http.route", "in", ["/health", "/ready"]))]
    doc = spans_doc(span("GET /health", "/health"), span("GET /cart", "/cart"), span("GET /ready", "/ready"))
    other = spans_doc(span("GET /health", "/health"), service="web")
    assert droprules.apply("traces", doc, rules) == (3, 2)
    assert droprules.apply("traces", other, rules) == (1, 0)
    assert [s["name"] for s in doc["resourceSpans"][0]["scopeSpans"][0]["spans"]] == ["GET /cart"]


def test_contains_is_case_insensitive_and_duration_is_numeric():
    fast = {"name": "Health check", "startTimeUnixNano": "1000", "endTimeUnixNano": "2000"}
    slow = {"name": "health check", "startTimeUnixNano": "0", "endTimeUnixNano": str(10**9)}
    doc = spans_doc(fast, slow)
    assert droprules.apply("traces", doc, [rule("traces", ("name", "contains", "HEALTH"), ("duration_ns", "<", 10**6))]) == (2, 1)


def test_metrics_by_name_and_point_attribute():
    def metric(name, *points):
        return {"name": name, "gauge": {"dataPoints": [{"asDouble": 1, "attributes": [kv("state", s)]} for s in points]}}
    doc = {"resourceMetrics": [{"resource": {"attributes": [kv("service.name", "host")]}, "scopeMetrics": [{"metrics": [
        metric("system.cpu.utilization", "idle", "user", "system"), metric("system.paging.faults", "major"),
        metric("system.memory.usage", "used")]}]}]}
    rules = [rule("metrics", ("metric_name", "=", "system.paging.faults")),
             rule("metrics", ("metric_name", "=", "system.cpu.utilization"), ("attributes.state", "=", "idle"))]
    assert droprules.apply("metrics", doc, rules) == (5, 2)
    names = {m["name"]: len(m["gauge"]["dataPoints"]) for m in doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    assert names == {"system.cpu.utilization": 2, "system.memory.usage": 1}   # a metric left with no points is gone


def test_sampling_keeps_whole_traces_together():
    spans = [{"name": "x", "traceId": f"{i:032x}"} for i in range(2000)] * 2   # every trace twice
    doc = spans_doc(*spans)
    received, dropped = droprules.apply("traces", doc, [rule("traces", ("name", "=", "x"), keep=10)])
    kept = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert received == 4000 and 250 < len(kept) < 550 and dropped == 4000 - len(kept)
    ids = [s["traceId"] for s in kept]
    assert all(ids.count(t) == 2 for t in set(ids))   # both spans of a kept trace are kept


def test_first_matching_rule_decides_and_disabled_or_broken_rules_are_ignored():
    doc = logs_doc({"severityNumber": 5}, {"severityNumber": 5, "attributes": [kv("keep", "yes")]})
    rules = [rule("logs", ("attributes.keep", "=", "yes"), keep=100), rule("logs", ("severity_number", "<", 9)),
             rule("logs", ("severity_number", "<", 100), enabled=False), {"signal": "logs", "conditions": "bad"},
             rule("traces", ("name", "=", "x"))]
    assert droprules.apply("logs", doc, rules) == (2, 1)


def test_no_rules_counts_everything():
    doc = logs_doc({"severityNumber": 1}, {"severityNumber": 2})
    assert droprules.apply("logs", doc, []) == (2, 0)
