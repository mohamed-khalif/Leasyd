"""The always-on hot store server (docs/hot-store.md), one Fargate task in the hot VPC:
  - reads every shard of the hot Kinesis stream (ingest puts each batch there: "<tenant> <signal>\\n" +
    the record it sent Firehose), from KEEP_S ago at start, into hotstore.HotStore;
  - drops rows older than KEEP_S every minute;
  - answers obs-query on port 8080 (reachable only from the query Lambda's security group):
      GET  /status   what it covers, rows held, how far behind the stream it is
      POST /worker   a JSON question about recent time -> a query worker's answer
      POST /scan     a SQL scan slice of recent time -> the kept rows (Parquet, base64)
      GET  /healthz  for the ECS health check
If it stops, obs-query asks the files about everything (slower, never wrong); on start it rereads the
stream from KEEP_S ago (the stream keeps 24 hours), so it needs no state of its own.
"""
import json
import os
import threading
import time
import traceback
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.exceptions import ClientError

import hotstore

STREAM = os.environ["HOT_STREAM"]
PORT = int(os.environ.get("PORT", "8080"))
kinesis = boto3.client("kinesis")
store = hotstore.HotStore()
behind = {}          # shard -> milliseconds behind the stream's tip, as last read
last_seq = {}        # shard -> the last sequence number read (to resume after an expired iterator)
started = time.time()


def consume(shard_id, first_iterator):
    """Read one shard until it closes (after a resharding its children carry on)."""
    it = first_iterator
    while it:
        try:
            r = kinesis.get_records(ShardIterator=it, Limit=10_000)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ProvisionedThroughputExceededException", "ThrottlingException"):
                time.sleep(1)
                continue
            if code == "ExpiredIteratorException" and last_seq.get(shard_id):
                it = kinesis.get_shard_iterator(StreamName=STREAM, ShardId=shard_id, ShardIteratorType="AFTER_SEQUENCE_NUMBER",
                                                StartingSequenceNumber=last_seq[shard_id])["ShardIterator"]
                continue
            raise
        groups, arrived = defaultdict(list), {}
        for rec in r["Records"]:
            head, _, body = rec["Data"].partition(b"\n")
            try:
                tenant, signal = head.decode().split(" ", 1)
            except ValueError:
                continue
            groups[(tenant, signal)].append(body)
            t = rec["ApproximateArrivalTimestamp"].timestamp()
            arrived[(tenant, signal)] = min(arrived.get((tenant, signal), t), t)
            last_seq[shard_id] = rec["SequenceNumber"]
        for (tenant, signal), bodies in groups.items():
            try:
                store.add(tenant, signal, bodies, arrived[(tenant, signal)])
            except Exception:   # noqa: BLE001  one bad batch must not stop the shard
                print(json.dumps({"hot_add_failed": tenant, "signal": signal, "error": traceback.format_exc()[-500:]}), flush=True)
        behind[shard_id] = r.get("MillisBehindLatest", 0)
        it = r.get("NextShardIterator")
        if not r["Records"]:
            time.sleep(0.5)
    behind.pop(shard_id, None)


def watch_shards():
    """Start a reader for every shard, now and as the stream reshards."""
    running, booted = set(), False
    while True:
        shards, kw = [], {"StreamName": STREAM}
        while True:
            page = kinesis.list_shards(**kw)
            shards += page["Shards"]
            if not page.get("NextToken"):
                break
            kw = {"NextToken": page["NextToken"]}
        for s in shards:
            sid = s["ShardId"]
            if sid in running:
                continue
            # At start: from KEEP_S ago. A shard born later (a resharding): all of it.
            start = ({"ShardIteratorType": "AT_TIMESTAMP", "Timestamp": store.reading_since} if not booted
                     else {"ShardIteratorType": "TRIM_HORIZON"})
            it = kinesis.get_shard_iterator(StreamName=STREAM, ShardId=sid, **start)["ShardIterator"]
            threading.Thread(target=consume, args=(sid, it), daemon=True).start()
            running.add(sid)
        booted = True
        time.sleep(30)


def evict_loop():
    while True:
        time.sleep(60)
        try:
            store.evict()
        except Exception:   # noqa: BLE001
            print(json.dumps({"hot_evict_failed": traceback.format_exc()[-500:]}), flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path == "/status":
            # Behind: the slowest shard; a reader not started yet counts as far behind.
            return self._send(200, {**store.status(), "behind_ms": max(behind.values(), default=10**9),
                                    "up_s": round(time.time() - started)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/worker":
                return self._send(200, store.worker(body))
            if self.path == "/scan":
                return self._send(200, store.scan(body))
            self._send(404, {"error": "not found"})
        except Exception as e:   # noqa: BLE001  the caller falls back to the files
            self._send(500, {"error": f"{type(e).__name__}: {str(e)[:300]}"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=watch_shards, daemon=True).start()
    threading.Thread(target=evict_loop, daemon=True).start()
    print(json.dumps({"hot_store": "serving", "port": PORT, "stream": STREAM, "keep_s": store.keep_s}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
