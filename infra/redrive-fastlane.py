#!/usr/bin/env python3
"""Replay fast-lane events that failed (queue obs-fastlane-failed).

    python3 infra/redrive-fastlane.py            # show what is queued
    python3 infra/redrive-fastlane.py --apply    # re-run obs-recent-indexer on each, delete the ones that succeed

Messages are either Lambda on-failure records (the original event is under
requestPayload) or events EventBridge could not deliver (the event itself).
Replaying is safe: the indexer is idempotent and skips files already
compacted (their raw file is gone). A file that is compacted meanwhile is
searchable anyway, so an old message is simply dropped as "already compacted".
"""

import argparse
import json

import boto3

QUEUE = "obs-fastlane-failed"
FUNCTION = "obs-recent-indexer"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    sqs, lam = boto3.client("sqs"), boto3.client("lambda")
    url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
    seen = ok = failed = 0
    while True:
        msgs = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                   VisibilityTimeout=300 if a.apply else 30).get("Messages", [])
        if not msgs:
            break
        for m in msgs:
            seen += 1
            body = json.loads(m["Body"])
            event = body.get("requestPayload", body)
            key = ((event.get("detail") or {}).get("object") or {}).get("key", "?")
            reason = (body.get("requestContext") or {}).get("condition") or body.get("responsePayload", {})
            if not a.apply:
                print(f"queued: {key}  ({json.dumps(reason)[:160]})")
                continue
            r = lam.invoke(FunctionName=FUNCTION, Payload=json.dumps(event).encode())
            out = r["Payload"].read().decode()
            if r.get("FunctionError"):
                failed += 1
                print(f"still failing: {key}: {out[:300]}")
            else:
                ok += 1
                sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
                print(f"replayed: {key}: {out[:200]}")
    print(f"{seen} message(s); replayed {ok}, still failing {failed}" if a.apply else
          f"{seen} message(s) queued (they stay, hidden for 30 s; run with --apply to replay)")


if __name__ == "__main__":
    main()
