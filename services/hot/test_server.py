"""The hot store server against moto's Kinesis: batches put the way ingest puts them are read from the
stream and answered over HTTP."""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import boto3
import pytest
from moto import mock_aws

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "compaction"), os.path.join(HERE, "..", "shared")]
os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", HOT_STREAM="obs-hot",
                  BUCKET="obs-data-test", INDEX_TABLE="obs-index", TENANT_READER_ROLE_ARN="arn:aws:iam::1:role/r")

H10 = 1790416800 * 10**9   # 2026-09-26T10:00:00Z


@pytest.fixture
def server():
    with mock_aws():
        sys.modules.pop("server", None)
        import server as srv
        k = boto3.client("kinesis")
        k.create_stream(StreamName="obs-hot", ShardCount=2)
        srv.store.reading_since = 0
        logs = {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
                                  "scopeLogs": [{"logRecords": [{"timeUnixNano": str(H10 + i * 10**9), "severityNumber": 9,
                                                                  "body": {"stringValue": f"hello {i}"}} for i in range(30)]}]}]}
        k.put_record(StreamName="obs-hot", PartitionKey="acme", Data=b"acme logs\n" + json.dumps(logs).encode() + b"\n")
        k.put_record(StreamName="obs-hot", PartitionKey="bad", Data=b"not a batch")   # skipped, not fatal
        threading.Thread(target=srv.watch_shards, daemon=True).start()
        for _ in range(100):
            if srv.store.status()["rows"].get("hot_logs"):
                break
            time.sleep(0.1)
        http = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{http.server_address[1]}"
        http.shutdown()


def call(url, path, body=None):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_reads_the_stream_and_answers_over_http(server):
    st = call(server, "/status")
    assert st["rows"]["hot_logs"] == 30 and "acme" in st["covered"] and st["behind_ms"] < 10**9
    out = call(server, "/worker", {"tenant": "acme", "signal": "logs", "start": "2026-09-26T10:00:00Z",
                                   "end": "2026-09-26T10:00:09Z", "aggs": [{"fn": "count"}]})
    assert out["rows"] == [[10]]   # 10:00:00 to 10:00:09, both ends included
    assert call(server, "/healthz") == {"ok": True}


def test_a_bad_question_is_an_error_not_a_crash(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server, "/worker", {"tenant": "NOT A TENANT", "signal": "logs"})
    assert e.value.code == 500
    assert call(server, "/healthz") == {"ok": True}
