"""Runs the investigation agent over the eval cases and scores it.

  accuracy               the root cause names the known cause (or, where nothing can be known,
                         says "not sure" or an accepted honest answer) and none of the case's wrong turns
  evidence hit           the finding cites evidence that showed the cause (cases with a known cause)
  evidence precision     the share of cited evidence that held any data
  false confidence       wrong answers given with confidence 0.5 or more, over all cases

The model is a port: live (Claude, as deployed), recorded (live, saving each response) or replayed
(a saved run played back, no model calls: checks the code around the model, not the prompt).

  python -m eval.harness --live [--record]          needs CLAUDE_MODEL and an API key or Bedrock access
  python -m eval.harness --replay                   from eval/recordings/
  python -m eval.harness --live --only dns_elsewhere --json report.json
"""

import argparse
import json
import os
import sys
from datetime import datetime

from agent.claude_agent import ClaudeAgent
from agent.prompts import BuiltIn
from domain.model import Investigation, Trigger
from eval.cases import CASES, NOW, STARTED
from eval.fixtures import FixtureTelemetry

RECORDINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
CONFIDENT = 0.5


class Recorder:
    """A live model that saves what it answered, to replay later."""

    def __init__(self, inner):
        self.inner, self.model, self.turns = inner, inner.model, []

    def create(self, **kw):
        resp = self.inner.create(**kw)
        self.turns.append(resp)
        return resp

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"model": self.model, "turns": self.turns}, f, indent=1)


class Replay:
    def __init__(self, path):
        with open(path) as f:
            saved = json.load(f)
        self.model, self.turns = saved["model"], list(saved["turns"])

    def create(self, **kw):
        if not self.turns:
            raise RuntimeError("the recording ran out: the agent asked more than the recorded run did")
        return self.turns.pop(0)


def _has(text, words):
    return any(w.lower() in text for w in words)


def score(case, result, telemetry):
    exp = case["expect"]
    root = result.root_cause.lower()
    not_sure = root.startswith("not sure")
    if exp["kind"] == "cause":
        correct = all(_has(root, group) for group in exp["cause"])
    else:
        correct = not_sure or any(_has(root, group) for group in exp.get("accept", []))
    if _has(root, exp.get("forbid", [])):
        correct = False
    calls = telemetry.log            # the n-th telemetry call is evidence ev<n>
    cited = [int(e.id[2:]) - 1 for e in result.evidence if e.id.startswith("ev") and e.id[2:].isdigit()]
    cited = [calls[i] for i in cited if i < len(calls)]
    has_key = any(c["key"] for c in calls) or bool(case.get("probes"))
    return {
        "id": case["id"], "kind": exp["kind"], "correct": correct, "confidence": result.confidence,
        "false_confidence": not correct and result.confidence >= CONFIDENT,
        "evidence_hit": (any(c["key"] for c in cited) if has_key and exp["kind"] == "cause" else None),
        "evidence_precision": (round(sum(c["data"] for c in cited) / len(cited), 2) if cited else None),
        "root_cause": result.root_cause, "cited": [e.id for e in result.evidence],
    }


def run_case(case, llm, prompts=None):
    telemetry = FixtureTelemetry(case)
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    agent = ClaudeAgent(llm, lambda tenant, start, end: telemetry, prompts or BuiltIn(), "https://app.leasyd.com",
                        {"input": 5, "output": 25}, now=lambda: now)
    a = case["alert"]
    inv = Investigation(id=case["id"], tenant="eval", status="running", created_at=NOW, updated_at=NOW,
                        trigger=Trigger(source="alert", title=a["title"], dedupe_key=case["id"], started_at=STARTED,
                                        subject=a.get("subject", ""), service=a.get("service", ""), severity=a.get("severity", ""),
                                        details=a.get("details", "")))
    result = agent.investigate(inv)
    return {**score(case, result, telemetry), "tool_calls": len(inv.tool_calls), "cost": inv.cost, "versions": agent.versions}


def summarize(rows):
    n = len(rows)
    hits = [r["evidence_hit"] for r in rows if r["evidence_hit"] is not None]
    prec = [r["evidence_precision"] for r in rows if r["evidence_precision"] is not None]
    return {"cases": n, "accuracy": round(sum(r["correct"] for r in rows) / n, 2),
            "evidence_hit": round(sum(hits) / len(hits), 2) if hits else None,
            "evidence_precision": round(sum(prec) / len(prec), 2) if prec else None,
            "false_confidence_rate": round(sum(r["false_confidence"] for r in rows) / n, 2),
            "usd": round(sum((r["cost"] or {}).get("usd", 0) for r in rows), 4)}


def run_all(llm_for, cases=CASES, prompts=None, after=None):
    rows = []
    for case in cases:
        llm = llm_for(case)
        rows.append(run_case(case, llm, prompts))
        if after:
            after(case, llm)
    return {"summary": summarize(rows), "cases": rows}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", action="store_true")
    p.add_argument("--record", action="store_true", help="with --live: save each case's responses to eval/recordings/")
    p.add_argument("--only", help="comma-separated case ids")
    p.add_argument("--json", help="write the full report here")
    args = p.parse_args(argv)
    cases = [c for c in CASES if not args.only or c["id"] in args.only.split(",")]
    path = lambda case: os.path.join(RECORDINGS, f"{case['id']}.json")     # noqa: E731

    if args.live:
        from adapters.claude import AnthropicLLM
        model = os.environ.get("CLAUDE_MODEL") or sys.exit("set CLAUDE_MODEL")
        live = AnthropicLLM(model, os.environ.get("BEDROCK_REGION", "us-east-1"), os.environ.get("ANTHROPIC_KEY_PARAM", ""),
                            api_key=os.environ.get("ANTHROPIC_API_KEY"))
        llm_for = (lambda case: Recorder(live)) if args.record else (lambda case: live)
        after = (lambda case, llm: llm.save(path(case))) if args.record else None
    else:
        llm_for, after = (lambda case: Replay(path(case))), None
    report = run_all(llm_for, cases, after=after)
    for r in report["cases"]:
        print(f"{'ok ' if r['correct'] else 'BAD'} {r['id']:<15} conf {r['confidence']:.2f}  hit {r['evidence_hit']!s:<5} "
              f"cited {','.join(r['cited']) or '-':<14} {r['root_cause'][:70]}")
    print(json.dumps(report["summary"]))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1, default=str)


if __name__ == "__main__":
    main()
