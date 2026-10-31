"""Builds the ports for the Lambda handlers from the environment (once per container)."""

import os

from adapters.aws import AwsChannels, AwsStore, SqsQueue, TenantDeploys
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
API_URL = os.environ.get("API_URL", "https://ingest.leasyd.com")   # where webhooks are sent, feedback links point
KMS_KEY = os.environ.get("KMS_KEY", "alias/obs-checks")         # alert channels' URLs and secrets
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")                   # SES sender; empty: no email to own users
FEEDBACK_TTL_S = int(os.environ.get("FEEDBACK_TTL_DAYS", "14")) * 86400

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
                                          SsmPrompts(PROMPT_PARAM), APP_URL, PRICES,
                                          deploys_for=lambda tenant: TenantDeploys(store(), tenant))
        else:
            raise RuntimeError(f"AGENT={AGENT!r}: stub or claude")
    return _built["agent"]


def channels():
    if "channels" not in _built:
        _built["channels"] = AwsChannels(TABLE, KMS_KEY)
    return _built["channels"]


def senders():
    if "senders" not in _built:
        import boto3

        from adapters.channels import senders as make
        from adapters.http import post
        ses, sns = boto3.client("sesv2"), boto3.client("sns")

        def ses_send(to, subject, text, html):
            ses.send_email(FromEmailAddress=EMAIL_FROM, Destination={"ToAddresses": [to]},
                           Content={"Simple": {"Subject": {"Data": subject, "Charset": "UTF-8"},
                                               "Body": {"Text": {"Data": text, "Charset": "UTF-8"}, "Html": {"Data": html, "Charset": "UTF-8"}}}})

        def sns_publish(topic, subject, text):
            sns.publish(TopicArn=topic, Subject=subject, Message=text)
        _built["senders"] = make(post, ses_send if EMAIL_FROM else None, sns_publish)
    return _built["senders"]


def feedback_links(tenant, inv_id):
    """Right/Wrong links for one delivery: one new single-use token (either answer uses it up)."""
    token = store().issue_feedback(tenant, inv_id, clock().now(), FEEDBACK_TTL_S)
    url = f"{API_URL}/v1/investigations/feedback/{token}"
    return {"right": f"{url}?v=right", "wrong": f"{url}?v=wrong"}


def clock():
    return SystemClock()
