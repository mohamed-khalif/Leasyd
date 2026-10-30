"""The investigator's records, as plain data: an Investigation (what triggered it, where it is in its
life, what happened to it) and its channel-neutral Result. Pure Python: no AWS, no model client.

Life of an investigation (TRANSITIONS):

    queued ──▶ running ──▶ completed
       │          │
       │          └──────▶ failed        (gave up after its attempts)
       └──▶ skipped                      (not investigated: e.g. the tenant's hourly cap; still delivered as a notice)

Every change is also an entry in its append-only event log (`events`), the audit trail of what was
done to it and when. `version` counts saves, so a store can refuse a stale write (two workers on one
message).
"""

from dataclasses import asdict, dataclass, field

QUEUED, RUNNING, COMPLETED, FAILED, SKIPPED = "queued", "running", "completed", "failed", "skipped"
TRANSITIONS = {QUEUED: {RUNNING, SKIPPED, FAILED}, RUNNING: {COMPLETED, FAILED}, COMPLETED: set(), FAILED: set(), SKIPPED: set()}
FINAL = {COMPLETED, FAILED, SKIPPED}
SOURCES = ("alert", "generic", "pagerduty", "cloudwatch")


class InvalidTransition(ValueError):
    pass


class InvalidResult(ValueError):
    pass


@dataclass
class Trigger:
    """What started an investigation, normalised from wherever it came from."""
    source: str                    # SOURCES
    title: str                     # "Checkout errors: checkout" / the alarm's or incident's name
    dedupe_key: str                # the same key within the dedupe window joins the open investigation
    started_at: str                # ISO-8601 UTC: when the problem started (as the source says), else when it arrived
    subject: str = ""              # what is wrong: a service, a check, an alarm
    service: str | None = None     # the tenant's service, when the source names one
    severity: str = "critical"     # critical | warning | info
    rule_id: str | None = None     # a Leasyd alert rule
    url: str | None = None         # where to see it at the source
    details: str = ""              # the source's own description (shown to the agent as data, cut short)

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"source: one of {SOURCES}")
        self.title, self.details = self.title.strip()[:200], self.details.strip()[:2000]


@dataclass
class Evidence:
    """One piece of evidence: what was looked at (the query), over which window, what it showed, and a
    link back to that data in Leasyd. Cited by id from the Result."""
    id: str
    kind: str                      # metrics | logs | traces | changes | context | past_incident
    title: str
    summary: str
    link: str
    query: dict = field(default_factory=dict)
    start: str | None = None
    end: str | None = None


@dataclass
class Result:
    """The finished outcome, the same for every channel: an adapter renders it (Slack, Teams, email,
    webhook) and adds the feedback links."""
    title: str
    summary: str
    root_cause: str                # "not sure" when the evidence doesn't support one
    confidence: float              # 0-1
    evidence: list[Evidence] = field(default_factory=list)
    ruled_out: list[str] = field(default_factory=list)
    next_step: str = ""
    safe_to_auto_act: bool = False  # always False in this phase (no actions)

    def validate(self):
        """Raises InvalidResult when the result can't be delivered as is. Forces safe_to_auto_act off."""
        self.safe_to_auto_act = False
        if not self.title.strip() or not self.summary.strip() or not self.root_cause.strip():
            raise InvalidResult("title, summary and root_cause are required")
        if not 0 <= float(self.confidence) <= 1:
            raise InvalidResult("confidence: between 0 and 1")
        ids = [e.id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise InvalidResult("evidence ids must be unique")
        if self.confidence > 0 and not self.evidence:
            raise InvalidResult("a root cause with confidence above 0 must cite evidence")
        return self


@dataclass
class Investigation:
    id: str
    tenant: str
    trigger: Trigger
    status: str
    created_at: str
    updated_at: str
    events: list[dict] = field(default_factory=list)    # {at, type, detail}
    result: Result | None = None
    skip_reason: str | None = None
    attempts: int = 0
    versions: dict = field(default_factory=dict)         # agent, model, prompt, tools: what produced the result
    cost: dict = field(default_factory=dict)             # input_tokens, output_tokens, usd
    version: int = 0                                     # saves so far (optimistic locking)

    def log(self, at, kind, detail=""):
        self.events.append({"at": at, "type": kind, "detail": str(detail)[:500]})
        self.updated_at = at

    def transition(self, to, at, detail=""):
        if to not in TRANSITIONS[self.status]:
            raise InvalidTransition(f"{self.status} -> {to}")
        self.log(at, to, detail)
        self.status = to

    @property
    def final(self):
        return self.status in FINAL

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        d = dict(d)
        d["trigger"] = Trigger(**d["trigger"])
        if d.get("result"):
            r = dict(d["result"])
            r["evidence"] = [Evidence(**e) for e in r.get("evidence", [])]
            d["result"] = Result(**r)
        return Investigation(**d)
