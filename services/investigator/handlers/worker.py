"""obs-investigator-worker: the queue's consumer (SQS, one message at a time, partial batch failures).

  {"run": {"tenant", "id"}}       run the investigation (domain.workflow.run_investigation); an attempt
                                  that raises is reported failed so SQS retries it, and the last one
                                  (ApproximateReceiveCount = MAX_ATTEMPTS) marks it failed
  {"deliver": {"tenant", "id"}}   send the finished (or skipped, or failed) investigation to its
                                  channels (domain.delivery.deliver): each channel retried, then the
                                  fallback; what was sent is recorded, so a redelivery sends nothing twice
"""

import json
import time

from domain.delivery import deliver
from domain.workflow import iso, run_investigation
from handlers import deps


def handler(event, context=None):
    failures = []
    for rec in event.get("Records") or []:
        try:
            msg = json.loads(rec["body"])
            if "run" in msg:
                attempt = int((rec.get("attributes") or {}).get("ApproximateReceiveCount", "1"))
                out = run_investigation(deps.store(), deps.agent(), deps.queue(), deps.clock(),
                                        msg["run"]["tenant"], msg["run"]["id"], attempt)
                print(json.dumps({"investigation_run": msg["run"]["id"], "tenant": msg["run"]["tenant"], "outcome": out}))
            elif "deliver" in msg:
                d = msg["deliver"]
                out = deliver(deps.store(), deps.channels(), deps.senders(), deps.clock(), iso, d["tenant"], d["id"],
                              lambda: deps.feedback_links(d["tenant"], d["id"]), time.sleep)
                print(json.dumps({"investigation_delivery": d["id"], "tenant": d["tenant"], "outcome": out}))
            else:
                print(json.dumps({"investigation_message_unknown": list(msg)}))
        except Exception as e:  # noqa: BLE001  this message is retried; the others in the batch are not
            print(json.dumps({"investigation_attempt_failed": rec.get("messageId"), "error": f"{type(e).__name__}: {str(e)[:300]}"}))
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}
