"""Memory: the tenant's past investigations, as leads for a new one.

Each investigation's index row keeps what memory needs (title, service, subject, rule, root cause,
confidence, and the team's Right/Wrong answer with its note). similar() ranks them against the new
trigger: the same rule, service or subject, words in common in the title, and how recent. A finding the
team marked wrong is kept, flagged, with what they said instead: the agent must not repeat it.

Past incidents are leads, never evidence for this incident: claude_agent refuses a root cause that
cites only them.
"""

import re
from dataclasses import replace
from datetime import datetime, timedelta

MIN_SCORE = 2.0
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")
_STOP = {"the", "and", "for", "with", "above", "below", "error", "errors", "rate", "alert", "high", "low", "live", "test"}


def _words(s):
    return {w for w in _WORD.findall(str(s or "").lower()) if w not in _STOP}


def _when(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def score(row, trigger, now, text=""):
    s = 0.0
    if trigger.rule_id and row.get("rule_id") == trigger.rule_id:
        s += 3
    svc = (trigger.service or "").lower()
    if svc and str(row.get("service") or "").lower() == svc:
        s += 2
    subj = (trigger.subject or "").lower()
    if subj and str(row.get("subject") or "").lower() == subj:
        s += 1.5
    mine, theirs = _words(f"{trigger.title} {text}"), _words(f"{row.get('title')} {row.get('root_cause')}")
    if mine and theirs:
        s += 3 * len(mine & theirs) / len(mine | theirs)
    when = _when(row.get("created_at"))
    if when and now - when <= timedelta(days=7):
        s += 0.5
    return s


def similar(rows, trigger, now, exclude_id=None, text="", service=None, limit=5):
    """Rows of past investigations (the store's index rows) -> the most similar finished ones, best first."""
    if service:
        trigger = replace(trigger, service=service)
    out = []
    for r in rows:
        if r.get("id") == exclude_id or r.get("status") != "completed" or not r.get("root_cause"):
            continue
        s = score(r, trigger, now, text)
        if s >= MIN_SCORE:
            out.append((s, r))
    out.sort(key=lambda x: str(x[1].get("created_at")), reverse=True)     # newest first among equals
    out.sort(key=lambda x: -x[0])
    return [_lead(r, s) for s, r in out[:limit]]


def _lead(r, s):
    lead = {"id": r["id"], "when": r.get("created_at"), "title": r.get("title"), "service": r.get("service") or r.get("subject"),
            "root_cause": r.get("root_cause"), "confidence": r.get("confidence"), "similarity": round(s, 1),
            "team_verdict": r.get("verdict") or "not rated"}
    if r.get("note"):
        lead["team_note"] = r["note"]
    if r.get("verdict") == "wrong":
        lead["warning"] = "The team marked this finding WRONG: don't repeat it without new evidence."
    return lead
