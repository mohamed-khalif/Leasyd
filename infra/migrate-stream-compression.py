#!/usr/bin/env python3
"""Switch every tenant ingest stream (obs-t-*) to pass records through
uncompressed, so ingest can gzip records itself (RecordCompression=gzip on
obs-phaseT2) and Firehose bills ~5x fewer bytes.

    python3 infra/migrate-stream-compression.py            # show what would change
    python3 infra/migrate-stream-compression.py --apply

Safe in any order with the ingest change: compaction, the fast lane and
query workers read raw files in every encoding of the changeover.
"""

import argparse
import os
import time

import boto3

fh = boto3.client("firehose", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def streams():
    names, start = [], None
    while True:
        kw = {"Limit": 100, "DeliveryStreamType": "DirectPut"}
        if start:
            kw["ExclusiveStartDeliveryStreamName"] = start
        resp = fh.list_delivery_streams(**kw)
        names += [n for n in resp["DeliveryStreamNames"] if n.startswith("obs-t-")]
        if not resp["HasMoreDeliveryStreams"]:
            return names
        start = resp["DeliveryStreamNames"][-1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    changed = done = 0
    for name in streams():
        d = fh.describe_delivery_stream(DeliveryStreamName=name)["DeliveryStreamDescription"]
        dest = d["Destinations"][0]
        fmt = dest["ExtendedS3DestinationDescription"]["CompressionFormat"]
        if fmt == "UNCOMPRESSED":
            done += 1
            continue
        changed += 1
        print(f"{name}: {fmt} -> UNCOMPRESSED" + ("" if a.apply else " (dry run)"))
        if a.apply:
            for attempt in range(5):
                try:
                    fh.update_destination(
                        DeliveryStreamName=name, CurrentDeliveryStreamVersionId=d["VersionId"],
                        DestinationId=dest["DestinationId"],
                        ExtendedS3DestinationUpdate={"CompressionFormat": "UNCOMPRESSED"})
                    break
                except fh.exceptions.LimitExceededException:
                    time.sleep(2 ** attempt)
            time.sleep(0.25)   # UpdateDestination is rate limited
    print(f"{changed} streams {'switched' if a.apply else 'to switch'}, {done} already passing records through")


if __name__ == "__main__":
    main()
