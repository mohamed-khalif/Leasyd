"""Builds the ports for the Lambda handlers from the environment (once per container)."""

import os

from adapters.aws import AwsStore, SqsQueue
from agent.stub import StubAgent
from domain.workflow import HOURLY_CAP, SystemClock

TABLE = os.environ.get("TENANTS_TABLE", "obs-tenants")
BUCKET = os.environ.get("BUCKET", "")
QUEUE_URL = os.environ.get("QUEUE_URL", "")
AGENT = os.environ.get("AGENT", "stub")                      # stub (slice 1); the Claude agent from slice 2
CAP = int(os.environ.get("HOURLY_CAP", str(HOURLY_CAP)))    # investigations a tenant may start an hour
API_URL = os.environ.get("API_URL", "https://ingest.leasyd.com")   # where webhooks are sent

_built = {}


def store():
    if "store" not in _built:
        _built["store"] = AwsStore(TABLE, BUCKET)
    return _built["store"]


def queue():
    if "queue" not in _built:
        _built["queue"] = SqsQueue(QUEUE_URL)
    return _built["queue"]


def agent():
    if "agent" not in _built:
        if AGENT != "stub":
            raise RuntimeError(f"AGENT={AGENT!r}: only 'stub' exists in this version")
        _built["agent"] = StubAgent()
    return _built["agent"]


def clock():
    return SystemClock()
