"""Builds the ports for the Lambda handlers from the environment (once per container)."""

import os

from adapters.aws import AwsStore, SqsQueue
from agent.claude_agent import ClaudeAgent
from agent.stub import StubAgent
from domain.workflow import HOURLY_CAP, SystemClock

TABLE = os.environ.get("TENANTS_TABLE", "obs-tenants")
BUCKET = os.environ.get("BUCKET", "")
QUEUE_URL = os.environ.get("QUEUE_URL", "")
AGENT = os.environ.get("AGENT", "stub")                      # stub | claude
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-1")
ANTHROPIC_KEY_PARAM = os.environ.get("ANTHROPIC_KEY_PARAM", "/obs/anthropic/api_key")
PROMPT_PARAM = os.environ.get("PROMPT_PARAM", "")                # an SSM parameter replacing the built-in prompt
PRICES = {"input": float(os.environ.get("PRICE_INPUT_PER_MTOK", "5")), "output": float(os.environ.get("PRICE_OUTPUT_PER_MTOK", "25"))}
APP_URL = os.environ.get("APP_URL", "https://app.leasyd.com")
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
        if AGENT == "stub":
            _built["agent"] = StubAgent()
        elif AGENT == "claude":
            from adapters.claude import AnthropicLLM, SreTelemetry, SsmPrompts
            if not CLAUDE_MODEL:
                raise RuntimeError("AGENT=claude needs CLAUDE_MODEL")
            _built["agent"] = ClaudeAgent(AnthropicLLM(CLAUDE_MODEL, BEDROCK_REGION, ANTHROPIC_KEY_PARAM), SreTelemetry,
                                          SsmPrompts(PROMPT_PARAM), APP_URL, PRICES)
        else:
            raise RuntimeError(f"AGENT={AGENT!r}: stub or claude")
    return _built["agent"]


def clock():
    return SystemClock()
