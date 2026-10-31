"""The investigating agent: Claude with read-only tools, until it submits a finding.

Loop: the trigger (redacted, as data) -> the model -> its tool calls run (tools/telemetry.py, each an
Evidence record) -> ... -> submit_investigation, checked in code:
  - it must match the schema and cite only evidence ids the tools produced; a bad submission goes
    back to the model with the reason (SUBMIT_TRIES times), then the run ends "not sure";
  - safe_to_auto_act is forced off;
  - the cited Evidence comes from the tools (their links), never from the model's text.

Limits (all in code): MAX_STEPS model calls, a token budget, a wall-clock limit. Hitting one makes the
next call a forced submit_investigation with what it has; failing that, the result is "not sure"
with confidence 0. Every tool call (name, input, time, evidence id or error) and the cost go on the
investigation.
"""

import json
import time
from datetime import datetime, timedelta, timezone

from agent.redact import redact
from domain.model import Result
from tools.telemetry import SPECS, ToolRunner

SUBMIT = {"name": "submit_investigation", "description": "Your finding. Call once, at the end.", "input_schema": {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "root_cause", "confidence", "evidence", "ruled_out", "next_step"],
    "properties": {
        "summary": {"type": "string", "description": "two or three sentences: what happened, where, since when"},
        "root_cause": {"type": "string", "description": "the most likely cause, specific (service, operation, error); or \"not sure\""},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "description": "the evidence for the root cause", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "claim"],
            "properties": {"id": {"type": "string", "description": "an evidence id from a tool result, e.g. ev3"},
                           "claim": {"type": "string", "description": "what this evidence shows, with its numbers"}}}},
        "ruled_out": {"type": "array", "items": {"type": "string"}, "description": "plausible causes excluded, each with why"},
        "next_step": {"type": "string"},
        "safe_to_auto_act": {"type": "boolean", "description": "always false: no actions in this version"}}}}

TOOLS_VERSION = "telemetry-2"       # + get_recent_changes, get_service_context
MAX_STEPS = 16
TOKEN_BUDGET = 400_000          # input + output tokens across the run
TIME_LIMIT_S = 300
SUBMIT_TRIES = 2
MAX_TOKENS = 4096
BEFORE, AFTER = timedelta(minutes=60), timedelta(minutes=20)


class ClaudeAgent:
    name = "claude"

    def __init__(self, llm, telemetry_for, prompts, app_url, prices=None, clock=time.monotonic, now=None,
                 max_steps=MAX_STEPS, token_budget=TOKEN_BUDGET, time_limit_s=TIME_LIMIT_S, deploys_for=None):
        self.llm, self.telemetry_for, self.prompts, self.app_url = llm, telemetry_for, prompts, app_url
        self.deploys_for = deploys_for      # tenant -> its recorded deploys (ports.Deploys); None: inferred only
        self.prices = prices or {}          # USD per million tokens: {"input": x, "output": y}
        self.clock, self.now = clock, now or (lambda: datetime.now(timezone.utc))
        self.max_steps, self.token_budget, self.time_limit_s = max_steps, token_budget, time_limit_s
        self.versions = {}

    def window(self, inv):
        started = datetime.fromisoformat(inv.trigger.started_at.replace("Z", "+00:00"))
        return started - BEFORE, min(started + AFTER, self.now())

    def investigate(self, inv):
        prompt = self.prompts.load()
        self.versions = {"model": self.llm.model, "prompt": prompt["version"], "tools": TOOLS_VERSION}
        start, end = self.window(inv)
        tools = ToolRunner(self.telemetry_for(inv.tenant, start, end), start, end, self.app_url,
                           deploys=self.deploys_for(inv.tenant) if self.deploys_for else None)
        t = inv.trigger
        brief, _ = redact(json.dumps({"title": t.title, "source": t.source, "subject": t.subject, "service": t.service,
                                      "severity": t.severity, "started_at": t.started_at, "details": t.details}))
        messages = [{"role": "user", "content": (
            f"An alert fired. Investigate it now.\n<alert>\n{brief}\n</alert>\n"
            f"Your tools look at {start.strftime('%Y-%m-%dT%H:%M:%SZ')} to {end.strftime('%Y-%m-%dT%H:%M:%SZ')} by default "
            f"(an hour before it started to now, at most 20 minutes after). Current time: {self.now().strftime('%Y-%m-%dT%H:%M:%SZ')}.")}]
        usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        began, tries, finding = self.clock(), 0, None
        stopped = ""
        for step in range(self.max_steps + 1):
            over = (step >= self.max_steps and "step limit") or \
                   (usage["input_tokens"] + usage["output_tokens"] >= self.token_budget and "token budget") or \
                   (self.clock() - began >= self.time_limit_s and "time limit")
            if over:
                stopped = over
                messages.append({"role": "user", "content": f"Stop now ({over}): call submit_investigation with what you have. "
                                                            "If the evidence doesn't establish a cause, say \"not sure\"."})
            resp = self.llm.create(system=prompt["system"], messages=messages, max_tokens=MAX_TOKENS,
                                   tools=[SUBMIT] if over else [*SPECS, SUBMIT],
                                   tool_choice={"type": "tool", "name": "submit_investigation"} if over else None)
            usage["calls"] += 1
            for k in ("input_tokens", "output_tokens"):
                usage[k] += int((resp.get("usage") or {}).get(k) or 0)
            content = resp["content"]
            messages.append({"role": "assistant", "content": content})
            uses = [b for b in content if b.get("type") == "tool_use"]
            if not uses:
                if over:
                    break
                messages.append({"role": "user", "content": "Call submit_investigation with your finding now."})
                continue
            results = []
            for b in uses:
                if b["name"] == "submit_investigation":
                    try:
                        finding = self._finding(inv, b.get("input") or {}, tools.evidence)
                        results.append({"type": "tool_result", "tool_use_id": b["id"], "content": "Received."})
                    except ValueError as e:
                        tries += 1
                        results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                        "content": f"Not accepted: {e}. Fix it and call submit_investigation again."})
                elif over:
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": "No more queries."})
                else:
                    text, _ = tools.run(b["name"], b.get("input") or {})
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "content": text, **({"is_error": True} if text.startswith("Error:") else {})})
            messages.append({"role": "user", "content": results})
            if finding or tries >= SUBMIT_TRIES or over:
                break
        inv.tool_calls = tools.calls
        inv.cost = {**usage, "usd": round(usage["input_tokens"] / 1e6 * self.prices.get("input", 0)
                                          + usage["output_tokens"] / 1e6 * self.prices.get("output", 0), 4),
                    **({"stopped": stopped} if stopped else {}), **({"redacted": tools.redactions} if tools.redactions else {})}
        return finding or self._not_sure(inv, tools.evidence, stopped or ("no valid finding" if tries else "no finding"))

    def _finding(self, inv, f, evidence):
        """The model's submission -> a checked Result. Raises ValueError with what to fix."""
        missing = [k for k in SUBMIT["input_schema"]["required"] if k not in f]
        if missing:
            raise ValueError(f"missing {', '.join(missing)}")
        try:
            confidence = float(f["confidence"])
        except (TypeError, ValueError):
            raise ValueError("confidence must be a number from 0 to 1")
        cited = []
        for c in f["evidence"] if isinstance(f["evidence"], list) else []:
            ev = evidence.get(str((c or {}).get("id")))
            if ev is None:
                raise ValueError(f"evidence id {(c or {}).get('id')!r} is not one the tools returned "
                                 f"(you have: {', '.join(evidence) or 'none'})")
            if ev not in cited:
                ev.claim = str(c.get("claim") or "")[:500]
                cited.append(ev)
        root = str(f["root_cause"]).strip()
        not_sure = root.lower().startswith("not sure")
        if not not_sure and not cited:
            raise ValueError("a root cause must cite evidence; if you can't, say \"not sure\"")
        if not_sure and confidence > 0.3:
            confidence = 0.3
        result = Result(title=inv.trigger.title, summary=str(f["summary"])[:2000], root_cause=root[:1000], confidence=confidence,
                        evidence=cited, ruled_out=[str(x)[:300] for x in (f.get("ruled_out") or [])][:10],
                        next_step=str(f["next_step"])[:1000])
        try:
            return result.validate()
        except Exception as e:  # noqa: BLE001  the model gets the reason
            raise ValueError(str(e))

    def _not_sure(self, inv, evidence, why):
        looked = list(evidence.values())[:6]
        return Result(title=inv.trigger.title, root_cause="not sure", confidence=0.0, evidence=looked,
                      summary=f"The automatic investigation didn't reach a finding ({why}). "
                              f"It looked at {len(evidence)} pieces of evidence, linked below.",
                      next_step="Open the alert in Leasyd and look at the evidence below.").validate()
