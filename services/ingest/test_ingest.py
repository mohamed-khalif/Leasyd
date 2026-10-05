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


def test_records_gzipped_before_firehose_when_enabled(fh, monkeypatch, tmp_path):
    """Gzipped records concatenate into a multi-member gzip object that compaction reads."""
    import compact
    monkeypatch.setattr(ingest, "RECORD_COMPRESSION", "gzip")
    ingest.handler(event(logs_pb(n=5, service="api").SerializeToString()), None)
    ingest.handler(event(logs_pb(n=2500, service="big", body="b" * 1000).SerializeToString()), None)
    assert all(d[:2] == b"\x1f\x8b" for _, d in fh.puts)
    plain = sum(len(gzip.decompress(d)) for _, d in fh.puts)
    assert sum(len(d) for _, d in fh.puts) < plain / 5          # what Firehose bills
    obj = tmp_path / "firehose-object.json.gz"
    obj.write_bytes(b"".join(d for _, d in fh.puts))              # passed through, not recompressed
    written = compact.compact_logs([str(obj)], str(tmp_path / "out"), "b1", "2026-09-26", "20")
    assert sorted((w["service"], w["rows"]) for w in written) == [("api", 5), ("big", 2500)]


def test_same_parquet_with_and_without_record_compression(monkeypatch, tmp_path):
    """The same requests, sent with RECORD_COMPRESSION off and on, compact to identical Parquet."""
    import compact
    import duckdb
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)
    reqs = [logs_pb(n=300, service="api").SerializeToString(),
            logs_pb(n=2500, service="big", body="payload " * 150).SerializeToString()]
    out = {}
    for mode in ("none", "gzip"):
        fh = FakeFirehose()
        monkeypatch.setattr(ingest, "firehose", fh)
        monkeypatch.setattr(ingest, "RECORD_COMPRESSION", mode)
        for body in reqs:
            assert ingest.handler(event(body), None)["statusCode"] == 200
        data = b"".join(d for _, d in fh.puts)
        obj = tmp_path / f"{mode}.json.gz"
        # Firehose gzips the object itself when records arrive plain (the old setup).
        obj.write_bytes(gzip.compress(data) if mode == "none" else data)
        written = compact.compact_logs([str(obj)], str(tmp_path / mode), "b1", "2026-09-26", "20")
        out[mode] = {w["service"]: (duckdb.sql(f"SELECT * FROM read_parquet('{w['path']}') ORDER BY ts_unix_nano")
                                    .fetchall(), w["bloom"].to_bytes()) for w in written}
        out[mode + "_bytes"] = len(data)
    assert out["gzip"] == out["none"] and sorted(out["none"]) == ["api", "big"]
    assert out["gzip_bytes"] < out["none_bytes"] / 4   # what Firehose would bill


# ------------------------------------------------------------------ metering and daily caps

@pytest.fixture
def metered(fh, monkeypatch):
    import boto3
    from moto import mock_aws
    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(TableName="obs-tenants", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
        monkeypatch.setattr(ingest, "ddb", ddb)
        monkeypatch.setattr(ingest, "TENANTS_TABLE", "obs-tenants")
        monkeypatch.setattr(ingest, "meter", ingest.Meter())
        yield ddb


def meter_item(ddb, tenant):
    it = ddb.get_item(TableName="obs-tenants", Key={"pk": {"S": f"meter#{tenant}#{ingest._day()}"}}).get("Item") or {}
    return {k: int(v["N"]) for k, v in it.items() if "N" in v}


def test_bytes_are_metered_and_a_daily_cap_refuses_until_midnight(metered, fh, monkeypatch):
    ddb = metered
    ddb.put_item(TableName="obs-tenants", Item={"pk": {"S": "tenant#acme"}, "daily_cap_bytes": {"N": "2000"}})
    body = logs_pb(n=3, body="x" * 200).SerializeToString()
    assert ingest.handler(event(body), None)["statusCode"] == 200
    ingest.meter.flush()
    m = meter_item(ddb, "acme")
    assert m["records"] == 3 and m["bytes"] == sum(len(r) for _, r in fh.puts) and m["refused_bytes"] == 0
    # Accepted until today's bytes reach the cap (this container's unflushed bytes count too)...
    statuses = [ingest.handler(event(body), None)["statusCode"] for _ in range(5)]
    assert statuses[0] == 200 and statuses[-1] == 429
    out = ingest.handler(event(body), None)
    assert out["statusCode"] == 429 and "daily data limit reached (2 KB a day" in out["body"]
    assert 0 < int(out["headers"]["Retry-After"]) <= 86400
    ingest.meter.flush()
    assert meter_item(ddb, "acme")["refused_bytes"] > 0
    # ...for that tenant only; one without a cap is metered, never refused.
    for _ in range(5):
        assert ingest.handler(event(body, tenant="beta"), None)["statusCode"] == 200
    ingest.meter.flush()
    assert meter_item(ddb, "beta")["records"] == 15


def test_an_ended_trial_refuses_data(metered, fh):
    ddb = metered
    ddb.put_item(TableName="obs-tenants", Item={"pk": {"S": "tenant#acme"}, "trial_ends_at": {"S": "2099-01-01T00:00:00Z"}})
    ddb.put_item(TableName="obs-tenants", Item={"pk": {"S": "tenant#beta"}, "trial_ends_at": {"S": "2020-01-01T00:00:00Z"}})
    body = logs_pb(n=2).SerializeToString()
    assert ingest.handler(event(body), None)["statusCode"] == 200                  # trial still running
    out = ingest.handler(event(body, tenant="beta"), None)
    assert out["statusCode"] == 403 and "free trial ended on 2020-01-01" in out["body"] and "mkhalif@leasyd.com" in out["body"]
    assert all(stream.endswith("acme-logs") for stream, _ in fh.puts)            # nothing of beta's was stored
    ingest.meter.flush()
    assert meter_item(ddb, "beta")["refused_bytes"] > 0


def test_an_ended_subscription_refuses_data(metered, fh):
    ddb = metered
    ddb.put_item(TableName="obs-tenants", Item={"pk": {"S": "tenant#acme"}, "billing_status": {"S": "past_due"}})
    ddb.put_item(TableName="obs-tenants", Item={"pk": {"S": "tenant#beta"}, "billing_status": {"S": "canceled"}})
    body = logs_pb(n=2).SerializeToString()
    assert ingest.handler(event(body), None)["statusCode"] == 200                  # still billed: accepted
    out = ingest.handler(event(body, tenant="beta"), None)
    assert out["statusCode"] == 403 and "subscription has ended (canceled)" in out["body"] and "Settings > Billing" in out["body"]
    assert all(stream.endswith("acme-logs") for stream, _ in fh.puts)


def test_metering_never_fails_a_request(fh, monkeypatch):
    class Broken:
        def get_item(self, **kw): raise RuntimeError("DynamoDB is down")
        def update_item(self, **kw): raise RuntimeError("DynamoDB is down")
    monkeypatch.setattr(ingest, "ddb", Broken())
    monkeypatch.setattr(ingest, "TENANTS_TABLE", "obs-tenants")
    monkeypatch.setattr(ingest, "meter", ingest.Meter())
    monkeypatch.setattr(ingest, "METER_FLUSH_S", 0)
    assert ingest.handler(event(logs_pb().SerializeToString()), None)["statusCode"] == 200
    assert ingest.meter.pending                               # kept for the next flush


def test_sizes_read_naturally():
    assert [ingest._size(n) for n in (10**9, 200_000, 2_500_000, 512)] == ["1 GB", "200 KB", "2.5 MB", "512 bytes"]


def test_counts_metric_points():
    doc = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [
        {"name": "a", "gauge": {"dataPoints": [{}, {}]}}, {"name": "b", "histogram": {"dataPoints": [{}]}}]}]}]}
    assert ingest._count("metrics", doc) == 3


# ------------------------------------------------------------------ CloudWatch metric streams (Firehose)

def cw_line(name="Duration", dims=None, ns="AWS/Lambda", unit="Milliseconds", value=None, ts=H20 // 10**6):
    return {"metric_stream_name": "leasyd", "account_id": "123456789012", "region": "eu-west-1",
            "namespace": ns, "metric_name": name, "dimensions": {"FunctionName": "checkout"} if dims is None else dims,
            "timestamp": ts, "value": value or {"max": 812.0, "min": 3.5, "sum": 4210.5, "count": 37.0}, "unit": unit}


def firehose_event(*records, gz=False, key_header=True):
    body = json.dumps({"requestId": "req-1", "timestamp": 1, "records": [
        {"data": base64.b64encode("".join(json.dumps(l) + "\n" for l in r).encode()).decode()} for r in records]}).encode()
    e = event(body, ctype="application/json", path="/v1/aws/cloudwatch-metrics", gz=gz,
              headers={"X-Amz-Firehose-Request-Id": "req-1"})
    return e


def test_cloudwatch_metric_stream_becomes_metrics(fh):
    out = ingest.handler(firehose_event(
        [cw_line(), cw_line("Invocations", unit="Count", value={"max": 1, "min": 1, "sum": 37, "count": 37})],
        [cw_line("ConcurrentExecutions", dims={}, unit="Count"),
         cw_line("Duration", dims={"FunctionName": "checkout", "Resource": "checkout:live"})], gz=True), None)
    assert out["statusCode"] == 200
    body = json.loads(out["body"])
    assert body["requestId"] == "req-1" and "errorMessage" not in body and body["timestamp"] > 0
    assert {s for s, _ in fh.puts} == {"obs-t-acme-metrics"}
    [doc] = lines(fh)
    res = {next(a["value"]["stringValue"] for a in r["resource"]["attributes"] if a["key"] == "service.name"): r
           for r in doc["resourceMetrics"]}
    assert set(res) == {"checkout", "aws-cloudwatch"}
    attrs = {a["key"]: a["value"]["stringValue"] for a in res["checkout"]["resource"]["attributes"]}
    assert attrs == {"service.name": "checkout", "cloud.provider": "aws", "cloud.account.id": "123456789012",
                     "cloud.region": "eu-west-1", "cloud.platform": "aws_lambda", "faas.name": "checkout"}
    metrics = {m["name"]: m for m in res["checkout"]["scopeMetrics"][0]["metrics"]}
    assert set(metrics) == {"aws.lambda.duration", "aws.lambda.invocations"}
    dur = metrics["aws.lambda.duration"]
    assert dur["unit"] == "ms" and dur["histogram"]["aggregationTemporality"] == 1
    p0, p1 = dur["histogram"]["dataPoints"]
    assert (p0["count"], p0["sum"], p0["min"], p0["max"], p0["timeUnixNano"]) == ("37", 4210.5, 3.5, 812.0, str(H20 // 10**6 * 10**6))
    assert {a["key"]: a["value"]["stringValue"] for a in p1["attributes"]} == {
        "aws.namespace": "AWS/Lambda", "aws.metric_name": "Duration", "FunctionName": "checkout", "Resource": "checkout:live"}
    assert metrics["aws.lambda.invocations"]["unit"] == "1"
    [conc] = res["aws-cloudwatch"]["scopeMetrics"][0]["metrics"]
    assert conc["name"] == "aws.lambda.concurrent_executions"


def test_cloudwatch_metrics_compact_like_any_histogram(fh, tmp_path):
    import compact
    import duckdb
    assert ingest.handler(firehose_event([cw_line()]), None)["statusCode"] == 200
    obj = tmp_path / "m.json.gz"
    obj.write_bytes(gzip.compress(b"".join(d for _, d in fh.puts)))
    [w] = compact.compact("metrics", [str(obj)], str(tmp_path / "out"), "b1", "2026-09-26", "20")
    got = duckdb.connect().execute(f"SELECT service, metric_name, metric_type, temporality, count, sum, min, max "
                                   f"FROM read_parquet('{w['path']}')").fetchall()
    assert got == [("checkout", "aws.lambda.duration", "histogram", 1, 37, 4210.5, 3.5, 812.0)]


@pytest.mark.parametrize("name,ns,expected", [
    ("Duration", "AWS/Lambda", "aws.lambda.duration"),
    ("ConcurrentExecutions", "AWS/Lambda", "aws.lambda.concurrent_executions"),
    ("CPUUtilization", "AWS/EC2", "aws.ec2.cpu_utilization"),
    ("ApproximateNumberOfMessagesVisible", "AWS/SQS", "aws.sqs.approximate_number_of_messages_visible"),
    ("Orders", "MyApp/Checkout", "aws.my_app.checkout.orders")])
def test_cloudwatch_metric_names(name, ns, expected):
    import cloudwatch
    assert cloudwatch.metric_name(ns, name) == expected


@pytest.mark.parametrize("body", [b"not json", b'{"records": "x"}',
                                  json.dumps({"records": [{"data": "%%%"}]}).encode(),
                                  json.dumps({"records": [{"data": base64.b64encode(b"opentelemetry bytes").decode()}]}).encode()])
def test_cloudwatch_bad_requests_answer_as_firehose_expects(fh, body):
    e = event(body, ctype="application/json", path="/v1/aws/cloudwatch-metrics", headers={"X-Amz-Firehose-Request-Id": "r9"})
    out = ingest.handler(e, None)
    assert out["statusCode"] == 400
    b = json.loads(out["body"])
    assert b["requestId"] == "r9" and b["errorMessage"] and not fh.puts


# ------------------------------------------------------------------ drop rules

def test_drop_rules_drop_and_meter_per_signal(metered, fh):
    ddb = metered
    rules = [{"id": "r1", "name": "Debug logs", "signal": "logs", "enabled": True, "keep_percent": 0,
              "conditions": [{"field": "body", "op": "contains", "value": "noise"}]}]
    ddb.put_item(TableName="obs-tenants", Item={"pk": {"S": "drop#acme"}, "rules_json": {"S": json.dumps(rules)}})
    assert ingest.handler(event(logs_pb(n=4, body="noise here").SerializeToString()), None)["statusCode"] == 200
    assert not fh.puts                                   # everything dropped: nothing stored
    assert ingest.handler(event(logs_pb(n=3, body="keep me").SerializeToString()), None)["statusCode"] == 200
    assert len(lines(fh)) == 1
    ingest.meter.flush()
    m = meter_item(ddb, "acme")
    assert m["records"] == 7 and m["in_logs"] == 7 and m["dropped_logs"] == 4
    assert m["bytes"] == sum(len(r) for _, r in fh.puts)   # stored bytes only


def test_unreadable_drop_rules_keep_everything(metered, fh):
    ddb = metered
    ddb.put_item(TableName="obs-tenants", Item={"pk": {"S": "drop#acme"}, "rules_json": {"S": "not json"}})
    assert ingest.handler(event(logs_pb(n=2).SerializeToString()), None)["statusCode"] == 200
    assert len(lines(fh)) == 1
