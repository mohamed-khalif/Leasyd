import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))

import canary  # noqa: E402
import ingest  # noqa: E402


def test_trace_ids_are_stable_hex_and_differ_by_signal_and_minute():
    t = canary.trace_id("logs", 100)
    assert t == canary.trace_id("logs", 100) and len(t) == 32 and int(t, 16) >= 0
    assert t != canary.trace_id("logs", 101) and t != canary.trace_id("traces", 100)


def test_records_are_accepted_by_ingest():
    for signal, top in (("logs", "resourceLogs"), ("traces", "resourceSpans")):
        body = json.dumps(canary.record(signal, 100, 6_000_000_000_000)).encode()
        doc = ingest.parse(signal, body, "application/json")
        assert len(doc[top]) == 1
        assert canary.trace_id(signal, 100) in json.dumps(doc)


def test_handler_publishes_missing_and_send_failures(monkeypatch):
    sent, metrics = [], []

    class Lam:
        def invoke(self, FunctionName, Payload):
            q = json.loads(Payload)
            found = q["signal"] == "logs"          # the traces record is missing
            return {"Payload": type("P", (), {"read": lambda self: json.dumps({"files": [1] if found else []})})()}

    class CW:
        def put_metric_data(self, Namespace, MetricData):
            metrics.extend(MetricData)

    monkeypatch.setattr(canary.boto3, "client", lambda name: {"lambda": Lam(), "cloudwatch": CW()}[name])
    monkeypatch.setattr(canary, "_send", lambda signal, body: sent.append(signal) or (200 if signal != "traces" else 403))
    monkeypatch.setattr(canary, "app_checks", lambda: {"web": 0, "api": 1})
    monkeypatch.setattr(canary.time, "time", lambda: 15 * 60 * 1_950_000 + 5.0)   # a minute divisible by 15
    out = canary.handler({}, None)
    assert sent == ["logs", "traces", "metrics"]
    assert out == {"logs": {"sent": 200, "missing": 0}, "traces": {"sent": 403, "missing": 1}, "metrics": {"sent": 200},
                   "app_down": {"web": 0, "api": 1}}
    got = {(m["MetricName"], m["Dimensions"][0]["Value"]): m["Value"] for m in metrics}
    assert got == {("CanaryMissing", "logs"): 0, ("CanarySendFailed", "logs"): 0,
                   ("CanaryMissing", "traces"): 1, ("CanarySendFailed", "traces"): 1, ("CanarySendFailed", "metrics"): 0,
                   ("AppDown", "web"): 0, ("AppDown", "api"): 1}
    # Other minutes: data checks only, no app checks.
    metrics.clear()
    monkeypatch.setattr(canary.time, "time", lambda: 15 * 60 * 1_950_000 + 65.0)
    assert "app_down" not in canary.handler({}, None)
    assert not any(m["MetricName"] == "AppDown" for m in metrics)


def test_app_checks(monkeypatch):
    monkeypatch.setattr(canary, "WEB_URL", "https://app.example")
    monkeypatch.setattr(canary, "ENDPOINT", "https://api.example")
    pages = {"https://app.example": (200, '<div id="root"></div>'), "https://api.example/v1/app/account": (401, "")}
    monkeypatch.setattr(canary, "_get", lambda url: pages[url])
    assert canary.app_checks() == {"web": 0, "api": 0}
    pages["https://app.example"] = (200, "<html>Access denied</html>")       # a page, but not the app
    pages["https://api.example/v1/app/account"] = (502, "")
    assert canary.app_checks() == {"web": 1, "api": 1}
    pages["https://api.example/v1/app/account"] = (200, "{}")               # open without sign-in: also wrong
    assert canary.app_checks()["api"] == 1


def test_metrics_are_accepted_by_ingest_and_compact_to_every_kind(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "compaction"))
    import gzip
    import duckdb
    import compact
    body = json.dumps(canary.metrics(29_300_000, 1_758_000_000 * 10**9, {"logs": 42.5, "traces": 130.0})).encode()
    assert len(ingest.parse("metrics", body, "application/json")["resourceMetrics"]) == 1
    raw = tmp_path / "m.json.gz"
    raw.write_bytes(gzip.compress(body))
    [w] = compact.compact("metrics", [str(raw)], str(tmp_path / "o"), "b1", "2025-09-16", "05")
    rows = duckdb.sql(f"SELECT metric_name, metric_type, temporality, is_monotonic, value, count, sum "
                      f"FROM '{w['path']}' ORDER BY 1, 5").fetchall()
    assert rows == [("canary.ingest.duration", "gauge", None, None, 42.5, None, None),
                    ("canary.ingest.duration", "gauge", None, None, 130.0, None, None),
                    ("canary.ingest.latency", "histogram", 1, None, None, 2, 172.5),
                    ("canary.runs", "sum", 2, True, 20_000.0, None, None)]


def test_route_checks_sign_in_and_call_every_backend(monkeypatch):
    # A function that can't start fails only when called: the route check calls each one.
    monkeypatch.setattr(canary, "ENDPOINT", "https://api.example")
    calls = []

    class Ssm:
        def get_parameter(self, Name, WithDecryption):
            return {"Parameter": {"Value": "pw"}}

    class Cognito:
        def initiate_auth(self, **kw):
            assert kw["AuthParameters"]["PASSWORD"] == "pw"
            return {"AuthenticationResult": {"IdToken": "tok"}}

    class Cw:
        def put_metric_data(self, Namespace, MetricData):
            calls.append(MetricData)
    monkeypatch.setattr(canary.boto3, "client", lambda name: {"ssm": Ssm(), "cognito-idp": Cognito(), "cloudwatch": Cw(),
                                                              "events": Events([])}[name])
    status = {"/v1/app/checks": 502}
    monkeypatch.setattr(canary, "_request", lambda method, url, token, body=None:
                        (assert_token(token), status.get(url[len("https://api.example"):], 200))[1])
    out = canary.handler({"check": "routes"}, None)
    assert out["routes"]["checks"] == 1 and sum(out["routes"].values()) == 1
    assert set(out["routes"]) == {"account", "checks", "slos", "windows", "alert-rules", "dashboards", "query"}
    total = [m for m in calls[0] if m["MetricName"] == "AppRoutesDown"]
    assert total == [{"MetricName": "AppRoutesDown", "Value": 1}]
    assert {"MetricName": "SchedulesOff", "Value": 0} in calls[0]

    class BadCognito:
        def initiate_auth(self, **kw):
            raise RuntimeError("NotAuthorizedException")
    monkeypatch.setattr(canary.boto3, "client", lambda name: {"ssm": Ssm(), "cognito-idp": BadCognito(), "cloudwatch": Cw()}[name])
    assert set(canary.route_checks().values()) == {1}                    # can't sign in: all down


class Events:
    def __init__(self, rules, fail=False):
        self.rules, self.fail = rules, fail

    def list_rules(self, NamePrefix, NextToken=None):
        if self.fail:
            raise RuntimeError("AccessDeniedException")
        assert NamePrefix == "obs-"
        page = 0 if NextToken is None else int(NextToken)
        out = {"Rules": self.rules[page * 2:page * 2 + 2]}
        if page * 2 + 2 < len(self.rules):
            out["NextToken"] = str(page + 1)
        return out


def test_switched_off_schedules_are_counted(monkeypatch):
    """Found 2026-10-13: the canary and synthetics schedules had been off for 9 days unnoticed."""
    monkeypatch.setattr(canary, "SCHEDULES_MAY_BE_OFF", {"obs-demo-schedule"})
    rules = [{"Name": "obs-canary-schedule", "State": "DISABLED", "ScheduleExpression": "rate(1 minute)"},
             {"Name": "obs-synthetics-schedule", "State": "ENABLED", "ScheduleExpression": "rate(1 minute)"},
             {"Name": "obs-demo-schedule", "State": "DISABLED", "ScheduleExpression": "rate(1 minute)"},  # may be off
             {"Name": "obs-fastlane-rule", "State": "DISABLED", "EventPattern": "{}"},                   # not a schedule
             {"Name": "obs-alerts-schedule", "State": "DISABLED", "ScheduleExpression": "rate(1 minute)"}]
    assert canary.schedules_off(Events(rules)) == ["obs-alerts-schedule", "obs-canary-schedule"]   # across pages
    assert canary.schedules_off(Events([])) == []
    assert canary.schedules_off(Events([], fail=True)) is None
    calls = []

    class Cw:
        def put_metric_data(self, Namespace, MetricData):
            calls.append(MetricData)
    monkeypatch.setattr(canary, "route_checks", lambda: {"account": 0})
    canary.routes_handler(Cw(), Events([], fail=True))
    assert {"MetricName": "SchedulesOff", "Value": 1} in calls[0]          # unreadable counts as off: alarm


def assert_token(token):
    assert token == "tok"
