import base64
import gzip
import json
import os
import sys

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "compaction"))

import ingest  # noqa: E402

H20 = 1790452800 * 10**9
TRACE = bytes.fromhex("5b8efff798038103d269b633813fc60c")


class FakeFirehose:
    """Records puts; can fail the first N attempts of some records."""
    class exceptions:
        class ResourceNotFoundException(Exception): pass
        class ServiceUnavailableException(Exception): pass
        class ResourceInUseException(Exception): pass

    def __init__(self, fail_first=0, missing=False):
        self.puts, self.fail_first, self.missing, self.calls = [], fail_first, missing, 0

    def put_record_batch(self, DeliveryStreamName, Records):
        self.calls += 1
        if self.missing:
            raise self.exceptions.ResourceNotFoundException()
        out = []
        for r in Records:
            if self.fail_first > 0:
                self.fail_first -= 1
                out.append({"ErrorCode": "ServiceUnavailableException"})
            else:
                self.puts.append((DeliveryStreamName, r["Data"]))
                out.append({"RecordId": "x"})
        return {"FailedPutCount": sum(1 for o in out if "ErrorCode" in o), "RequestResponses": out}


@pytest.fixture
def fh(monkeypatch):
    f = FakeFirehose()
    monkeypatch.setattr(ingest, "firehose", f)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)
    return f


def kv(k, v):
    return KeyValue(key=k, value=AnyValue(string_value=v))


def logs_pb(n=3, service="api", body="hello", extra_resource=()):
    return ExportLogsServiceRequest(resource_logs=[ResourceLogs(
        resource=Resource(attributes=[kv("service.name", service), *extra_resource]),
        scope_logs=[ScopeLogs(log_records=[
            LogRecord(time_unix_nano=H20 + i, severity_number=9, severity_text="Info",
                      body=AnyValue(string_value=body), trace_id=TRACE,
                      attributes=[kv("request.id", f"r{i}")])
            for i in range(n)])])])


def event(body, ctype="application/x-protobuf", path="/v1/logs", tenant="acme", gz=False, headers=None):
    if gz:
        body = gzip.compress(body)
    h = {"Content-Type": ctype, **({"Content-Encoding": "gzip"} if gz else {}), **(headers or {})}
    ctx = {"authorizer": {"tenant": tenant}} if tenant else {}
    return {"path": f"/ingest{path}", "headers": h, "isBase64Encoded": True,
            "body": base64.b64encode(body).decode(), "requestContext": ctx}


def lines(fh):
    return [json.loads(l) for _, data in fh.puts for l in data.decode().splitlines()]


def test_protobuf_logs_to_tenant_stream(fh):
    out = ingest.handler(event(logs_pb().SerializeToString(), gz=True), None)
    assert out["statusCode"] == 200 and out["body"] == ""
    assert {s for s, _ in fh.puts} == {"obs-t-acme-logs"}
    [doc] = lines(fh)
    rec = doc["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert rec["traceId"] == TRACE.hex()             # hex, as OTLP JSON specifies
    assert rec["severityNumber"] == 9                # integer, not the enum name
    assert rec["timeUnixNano"] == str(H20)           # 64-bit ints as strings
    assert rec["body"] == {"stringValue": "hello"}


def test_json_body(fh):
    body = json.dumps({"resourceLogs": [{"resource": {"attributes": []},
                                         "scopeLogs": [{"logRecords": [{"body": {"stringValue": "j"}}]}]}]})
    out = ingest.handler(event(body.encode(), ctype="application/json"), None)
    assert out["statusCode"] == 200 and out["body"] == "{}"
    assert lines(fh)[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"] == {"stringValue": "j"}


def test_traces_go_to_traces_stream(fh):
    req = ExportTraceServiceRequest(resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[
        Span(trace_id=TRACE, span_id=bytes(8), parent_span_id=bytes.fromhex("00f067aa0ba902b7"), name="GET /")])])])
    assert ingest.handler(event(req.SerializeToString(), path="/v1/traces"), None)["statusCode"] == 200
    [(stream, _)] = fh.puts
    assert stream == "obs-t-acme-traces"
    span = lines(fh)[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == TRACE.hex() and span["parentSpanId"] == "00f067aa0ba902b7"


def test_tenant_only_from_authorizer(fh):
    spoof = logs_pb(extra_resource=[kv("obs.tenant", "globex"), kv("obs.anything", "x")])
    out = ingest.handler(event(spoof.SerializeToString(), headers={"x-obs-tenant": "globex"}), None)
    assert out["statusCode"] == 200
    assert {s for s, _ in fh.puts} == {"obs-t-acme-logs"}
    attrs = lines(fh)[0]["resourceLogs"][0]["resource"]["attributes"]
    assert [a["key"] for a in attrs] == ["service.name"]  # obs.* stripped


@pytest.mark.parametrize("tenant", [None, "", "Acme", "a/b", "x" * 41])
def test_missing_or_bad_tenant_rejected(fh, tenant):
    assert ingest.handler(event(logs_pb().SerializeToString(), tenant=tenant), None)["statusCode"] == 401
    assert fh.puts == []


@pytest.mark.parametrize("body,ctype,gz,status", [
    (b"\xff\xff\xff", "application/x-protobuf", False, 400),
    (b"not json", "application/json", False, 400),
    (b"[1]", "application/json", False, 400),
    (b"x", "text/plain", False, 415),
])
def test_bad_requests(fh, body, ctype, gz, status):
    assert ingest.handler(event(body, ctype=ctype, gz=gz), None)["statusCode"] == status
    assert fh.puts == []


def test_bad_gzip(fh):
    e = event(b"not gzip")
    e["headers"]["Content-Encoding"] = "gzip"
    assert ingest.handler(e, None)["statusCode"] == 400


def test_unknown_path(fh):
    assert ingest.handler(event(logs_pb().SerializeToString(), path="/v1/profiles"), None)["statusCode"] == 404


def test_large_request_split_under_record_limit(fh):
    big = logs_pb(n=3000, body="x" * 1000)  # ~3.5 MB as JSON
    assert ingest.handler(event(big.SerializeToString()), None)["statusCode"] == 200
    assert len(fh.puts) >= 4
    assert all(len(d) <= ingest.MAX_RECORD_BYTES for _, d in fh.puts)
    n = sum(len(sl["logRecords"]) for doc in lines(fh) for rl in doc["resourceLogs"] for sl in rl["scopeLogs"])
    assert n == 3000


def test_oversized_single_log_truncated(fh):
    huge = logs_pb(n=1, body="y" * 2_000_000)
    assert ingest.handler(event(huge.SerializeToString()), None)["statusCode"] == 200
    rec = lines(fh)[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert len(rec["body"]["stringValue"]) == ingest.TRUNCATED_BODY_BYTES
    assert {"key": "log.body.truncated", "value": {"boolValue": True}} in rec["attributes"]


def test_firehose_partial_failure_retried(fh):
    fh.fail_first = 2
    assert ingest.handler(event(logs_pb(n=1000, body="z" * 2000).SerializeToString()), None)["statusCode"] == 200
    assert fh.calls >= 2
    n = sum(len(sl["logRecords"]) for doc in lines(fh) for rl in doc["resourceLogs"] for sl in rl["scopeLogs"])
    assert n == 1000


def test_firehose_persistent_failure_is_503(fh):
    fh.fail_first = 10**6
    assert ingest.handler(event(logs_pb().SerializeToString()), None)["statusCode"] == 503


def test_unprovisioned_tenant_is_503(fh):
    fh.missing = True
    assert ingest.handler(event(logs_pb().SerializeToString()), None)["statusCode"] == 503


def test_compaction_reads_what_ingest_writes(fh, tmp_path):
    """Firehose concatenates records into a gzip object; compaction must read it."""
    import compact
    ingest.handler(event(logs_pb(n=5, service="api").SerializeToString()), None)
    ingest.handler(event(logs_pb(n=7, service="web").SerializeToString(), gz=True), None)
    ingest.handler(event(logs_pb(n=2500, service="big", body="b" * 1000).SerializeToString()), None)
    obj = tmp_path / "firehose-object.json.gz"
    obj.write_bytes(gzip.compress(b"".join(d for _, d in fh.puts)))
    written = compact.compact_logs([str(obj)], str(tmp_path / "out"), "b1", "2026-09-26", "20")
    assert sorted((w["service"], w["rows"]) for w in written) == [("api", 5), ("big", 2500), ("web", 7)]
    assert written[0]["bloom"].might_contain(compact.bloom.term("trace_id", TRACE.hex()))


def test_compaction_reads_traces_and_metrics_from_protobuf(fh, tmp_path):
    """Protobuf spans and metric points survive ingest's JSON conversion
    (hex ids, integer enums, int64s as strings) and compact to Parquet."""
    import compact
    import duckdb
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    from opentelemetry.proto.metrics.v1.metrics_pb2 import (
        AggregationTemporality, Gauge, Histogram, HistogramDataPoint, Metric, NumberDataPoint,
        ResourceMetrics, ScopeMetrics, Sum)
    from opentelemetry.proto.trace.v1.trace_pb2 import Status

    spans = ExportTraceServiceRequest(resource_spans=[ResourceSpans(
        resource=Resource(attributes=[kv("service.name", "api")]),
        scope_spans=[ScopeSpans(spans=[
            Span(trace_id=TRACE, span_id=bytes(range(8)), parent_span_id=bytes(range(1, 9)), name="GET /x",
                 kind=Span.SPAN_KIND_SERVER, start_time_unix_nano=H20 + i, end_time_unix_nano=H20 + i + 1500,
                 status=Status(code=Status.STATUS_CODE_ERROR, message="bad"),
                 events=[Span.Event(time_unix_nano=H20 + i + 10, name="e")])
            for i in range(4)])])])
    metrics = ExportMetricsServiceRequest(resource_metrics=[ResourceMetrics(
        resource=Resource(attributes=[kv("service.name", "api")]),
        scope_metrics=[ScopeMetrics(metrics=[
            Metric(name="cpu", gauge=Gauge(data_points=[NumberDataPoint(time_unix_nano=H20 + 1, as_double=0.25)])),
            Metric(name="reqs", sum=Sum(aggregation_temporality=AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE,
                                        is_monotonic=True,
                                        data_points=[NumberDataPoint(time_unix_nano=H20 + 2, as_int=2**40)])),
            Metric(name="lat", histogram=Histogram(
                aggregation_temporality=AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA,
                data_points=[HistogramDataPoint(time_unix_nano=H20 + 3, count=3, sum=7.5,
                                                bucket_counts=[1, 2], explicit_bounds=[5.0])])),
        ])])])
    for body, path in ((spans, "/v1/traces"), (metrics, "/v1/metrics")):
        assert ingest.handler(event(body.SerializeToString(), path=path), None)["statusCode"] == 200
    for signal in ("traces", "metrics"):
        obj = tmp_path / f"{signal}.json.gz"
        obj.write_bytes(gzip.compress(b"".join(d for s, d in fh.puts if s.endswith(signal))))
        [w] = compact.compact(signal, [str(obj)], str(tmp_path / signal), "b1", "2026-09-26", "20")
        con = duckdb.connect()
        con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{w['path']}')")
        if signal == "traces":
            assert w["rows"] == 4
            assert w["bloom"].might_contain(compact.bloom.term("trace_id", TRACE.hex()))
            assert con.execute("SELECT DISTINCT trace_id, span_id, parent_span_id, kind, status_code, "
                               "duration_ns, len(events) FROM t").fetchall() == [
                (TRACE.hex(), bytes(range(8)).hex(), bytes(range(1, 9)).hex(), 2, 2, 1500, 1)]
        else:
            got = con.execute("SELECT metric_name, metric_type, value, temporality, is_monotonic, count, sum, "
                              "bucket_counts, explicit_bounds FROM t ORDER BY metric_name").fetchall()
            assert got == [("cpu", "gauge", 0.25, None, None, None, None, None, None),
                           ("lat", "histogram", None, 1, None, 3, 7.5, [1, 2], [5.0]),
                           ("reqs", "sum", float(2**40), 2, True, None, None, None, None)]


def test_gzip_header_on_an_already_decompressed_body(fh):
    """API Gateway can decompress the body but keep Content-Encoding: gzip."""
    ev = event(logs_pb(n=3).SerializeToString(), headers={"Content-Encoding": "gzip"})
    assert ingest.handler(ev, None)["statusCode"] == 200
    assert len(lines(fh)) == 1
