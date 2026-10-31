"""The model and the investigation's other outside pieces on AWS.

AnthropicLLM     Claude on Amazon Bedrock (the Messages API endpoint, this function's role), or the Claude
                 API when an Anthropic API key is stored in SSM (ANTHROPIC_KEY_PARAM, as the AI SRE does).
                 The model id comes from configuration (CLAUDE_MODEL), never the code.
SreTelemetry     the AI SRE's query tools (services/ai/sre.py Tools), bound to one tenant and window;
                 queries go through obs-query, which reads only that tenant's data. Investigations
                 don't use up the tenant's daily search allowance (they have their own quota).
SsmPrompts       the investigation prompt from an SSM parameter ({"version", "system"}), if one is set;
                 else the built-in one.
"""

import json
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from agent.prompts import BuiltIn


class AnthropicLLM:
    def __init__(self, model, region, key_param="", api_key=None):
        key = api_key
        if key_param and not key:
            try:
                key = boto3.client("ssm").get_parameter(Name=key_param, WithDecryption=True)["Parameter"]["Value"]
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("ParameterNotFound", "AccessDeniedException"):
                    raise
        if key:
            from anthropic import Anthropic
            self.client, self.model = Anthropic(api_key=key, max_retries=3), model.removeprefix("anthropic.")
        else:
            from anthropic import AnthropicBedrockMantle
            self.client, self.model = AnthropicBedrockMantle(aws_region=region, max_retries=3), model

    def create(self, *, system, messages, tools, max_tokens, tool_choice=None):
        kw = {"model": self.model, "system": system, "messages": messages, "tools": tools, "max_tokens": max_tokens}
        if tool_choice:
            kw["tool_choice"] = tool_choice
        resp = self.client.messages.create(**kw)
        d = resp.to_dict()
        return {"content": d["content"], "stop_reason": d.get("stop_reason"), "usage": d.get("usage") or {}}


class SreTelemetry:
    def __init__(self, tenant, start: datetime, end: datetime):
        import sre       # the AI SRE's tools (copied into this bundle by build-investigator.sh)
        self.tools = sre.Tools(tenant, start, end, invoke=self._invoke)
        self._query = self.tools._invoke_query

    def _invoke(self, payload):
        return self._query({k: v for k, v in payload.items() if k != "bill"})

    def run(self, name, args):
        return self.tools.run(name, args)


class SsmPrompts:
    def __init__(self, param):
        self.param = param

    def load(self):
        if not self.param:
            return BuiltIn().load()
        p = json.loads(boto3.client("ssm").get_parameter(Name=self.param)["Parameter"]["Value"])
        if not p.get("system") or not p.get("version"):
            raise ValueError(f"{self.param}: needs version and system")
        return p
