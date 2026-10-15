"""Leasyd's query engine in one process, over moto (in-memory S3 and DynamoDB): load OTLP documents
for a tenant through the real fast lane (handler.recent_indexer), then run the real SQL and PromQL
on them. For tests (test_infra_queries.py) and the web app's local preview
(services/web/mock/engine_server.py), so the queries pages send run on the real engine.

    eng = LocalEngine()
    eng.load("metrics", [doc, doc, ...], at=datetime(...))     # one raw file (one JSON line per doc)
    eng.sql("SELECT count(*) FROM metrics", start, end)
    eng.close()
"""
import gzip
import importlib
import json
import os
import sys
from datetime import datetime, timezone

os.environ.update(
    BUCKET="obs-data-test", INDEX_TABLE="obs-index", USAGE_TABLE="obs-usage", WORKER_FUNCTION="obs-compaction-worker",
    AWS_DEFAULT_REGION="us-east-1", TENANT_READER_ROLE_ARN="arn:aws:iam::123456789012:role/obs-tenant-reader",
    AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", QUERY_WORKER_FUNCTION="obs-query-worker",
)
os.environ.pop("AWS_SESSION_TOKEN", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402


class LocalEngine:
    def __init__(self, tenant="acme"):
        self.tenant, self.files = tenant, 0
        self._aws = mock_aws()
        self._aws.start()
        import handler
        import lookup
        import query
        for m in (handler, lookup, query):
            importlib.reload(m)
        self.handler, self.lookup, self.query = handler, lookup, query
        query.lookup = lookup
        handler._invoke_worker = lambda payload: None
        boto3.client("s3").create_bucket(Bucket="obs-data-test")
        ddb = boto3.client("dynamodb")
        for name, keys in (("obs-index", ("pk", "sk")), ("obs-usage", ("tenant", "sk"))):
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in keys],
                             KeySchema=[{"AttributeName": keys[0], "KeyType": "HASH"}, {"AttributeName": keys[1], "KeyType": "RANGE"}])

    def load(self, signal, docs, at):
        """One raw (fast-lane) file of OTLP JSON documents ({"resourceMetrics": [...]} etc.), filed
        under the hour of `at` as Firehose would, made searchable at once."""
        self.files += 1
        at = at.astimezone(timezone.utc)
        key = (f"_incoming/tenant={self.tenant}/{signal}/dt={at:%Y-%m-%d}/hour={at:%H}/"
               f"obs-t-{self.tenant}-{signal}-1-{at:%Y-%m-%d-%H-%M-%S}-{self.files:06d}.json.gz")
        body = gzip.compress("\n".join(json.dumps(d, separators=(",", ":")) for d in docs).encode())
        boto3.client("s3").put_object(Bucket="obs-data-test", Key=key, Body=body)
        out = self.handler.recent_indexer({"detail": {"object": {"key": key}}}, None)
        if out.get("skipped"):
            raise RuntimeError(f"not indexed: {out}")
        return out

    def sql(self, text, start, end, services=None):
        out = self.query.run_sql(self.tenant, text, _iso(start), _iso(end), invoke_worker=self.query.run_sql_worker,
                                 services=services)
        if out.get("error"):
            raise self.query.BadQuery(out["error"])
        return out

    def records(self, text, start, end, services=None):
        out = self.sql(text, start, end, services)
        return [dict(zip(out["columns"], r)) for r in out["rows"]]

    def close(self):
        self._aws.stop()


def _iso(t):
    return t if isinstance(t, str) else t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_minutes(eng, docs_for_minute, minutes, per_file=15):
    """Load demo-style minutes ({signal: doc} per minute) in files of `per_file` minutes per signal."""
    batch, first = {}, None
    for i, m in enumerate(minutes):
        first = m if first is None else first
        for signal, doc in docs_for_minute(m).items():
            batch.setdefault(signal, []).append(doc)
        if (m + 1) % per_file == 0 or i == len(minutes) - 1:   # files never span an hour (per_file divides 60)
            at = datetime.fromtimestamp(first * 60, timezone.utc)
            for signal, docs in batch.items():
                eng.load(signal, docs, at)
            batch, first = {}, None
