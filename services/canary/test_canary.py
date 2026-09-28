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
    monkeypatch.setattr(canary, "_send", lambda signal, body: sent.append(signal) or (200 if signal == "logs" else 403))
    out = canary.handler({}, None)
    assert sent == ["logs", "traces"]
    assert out == {"logs": {"sent": 200, "missing": 0}, "traces": {"sent": 403, "missing": 1}}
    got = {(m["MetricName"], m["Dimensions"][0]["Value"]): m["Value"] for m in metrics}
    assert got == {("CanaryMissing", "logs"): 0, ("CanarySendFailed", "logs"): 0,
                   ("CanaryMissing", "traces"): 1, ("CanarySendFailed", "traces"): 1}
