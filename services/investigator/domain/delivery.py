"""Delivering an investigation's outcome to people. Pure: channels, senders, storage and time come in
through ports.

Outcome            one channel-neutral message: what every adapter (adapters/channels/) renders, in
                   its own format and as plain text.
choose_channels    where an investigation goes, from the tenant's settings:
                     1. the firing rule's own `investigation_channels`
                     2. else the settings' channels for the service (`services`)
                     3. else the settings' `channels` (the tenant's investigation channels)
                     4. else, for an alert, the rule's alert channels
                   only channels that exist; `fallback` when every one of them fails.
deliver            sends to each chosen channel not yet sent (a redelivered message sends nothing
                   twice), each up to ATTEMPTS times; when none got it, the fallback channel. Each
                   channel's outcome is recorded on the investigation. Raises only to have the queue
                   retry the whole delivery (a store conflict that kept happening).
"""

from dataclasses import asdict, dataclass, field

from domain.model import COMPLETED, FAILED, SKIPPED
from ports import StaleWrite

ATTEMPTS = 3
BACKOFF_S = (1, 3)          # waits between a channel's attempts


class DeliveryError(Exception):
    """A channel refused or couldn't be reached; the message says how (shown to the tenant)."""


@dataclass
class Outcome:
    kind: str                      # finding | skipped | failed
    title: str                     # the alert's
    headline: str                  # one line: the root cause, or why there's none
    summary: str
    investigation_id: str
    severity: str = "critical"
    service: str | None = None
    confidence: float | None = None
    confidence_label: str = ""     # high | medium | low | not sure
    root_cause: str = ""
    evidence: list[dict] = field(default_factory=list)     # {id, title, claim, link}
    ruled_out: list[str] = field(default_factory=list)
    next_step: str = ""
    alert_url: str | None = None   # where the alert came from
    feedback: dict | None = None   # {"right": url, "wrong": url}: findings only
    started_at: str = ""

    def to_dict(self):
        return asdict(self)


def confidence_label(c):
    return "high" if c >= 0.8 else "medium" if c >= 0.5 else "low" if c > 0.3 else "not sure"


def outcome(inv, feedback=None):
    t = inv.trigger
    base = dict(title=t.title, investigation_id=inv.id, severity=t.severity, service=t.service or t.subject or None,
                alert_url=t.url, started_at=t.started_at)
    if inv.status == SKIPPED:
        return Outcome(kind="skipped", headline="Not investigated: the hourly limit of automatic investigations was reached",
                       summary="This alert was sent as usual, but no automatic investigation ran for it: the account "
                               "started its maximum number of investigations this hour. Later alerts are investigated "
                               "again from the next hour.", **base)
    if inv.status == FAILED:
        return Outcome(kind="failed", headline="The automatic investigation couldn't finish",
                       summary="The investigation failed after several attempts, so there is no finding. Look at the "
                               "alert and its telemetry directly.", **base)
    if inv.status != COMPLETED or not inv.result:
        raise ValueError(f"nothing to deliver: investigation is {inv.status}")
    r = inv.result
    not_sure = r.root_cause.lower().startswith("not sure")
    return Outcome(kind="finding", headline=("Not sure: " + r.root_cause[len("not sure"):].lstrip(" :-—").strip()).rstrip(": ")
                   if not_sure else r.root_cause, summary=r.summary, confidence=r.confidence,
                   confidence_label=confidence_label(r.confidence), root_cause=r.root_cause,
                   evidence=[{"id": e.id, "title": e.title, "claim": e.claim, "link": e.link} for e in r.evidence],
                   ruled_out=list(r.ruled_out), next_step=r.next_step, feedback=feedback, **base)


def choose_channels(inv, settings, rule, exists):
    """-> (channel ids in order, fallback id or None). settings: {channels, services, fallback} or {};
    rule: the firing rule's item or None; exists: channel id -> bool."""
    settings = settings or {}
    svc = (inv.trigger.service or inv.trigger.subject or "").lower()
    by_service = {k.lower(): v for k, v in (settings.get("services") or {}).items()}
    for pick in ((rule or {}).get("investigation_channels"), by_service.get(svc), settings.get("channels"),
                 (rule or {}).get("channels")):
        chosen = [c for c in dict.fromkeys(pick or []) if exists(c)]
        if chosen:
            break
    else:
        chosen = []
    fallback = settings.get("fallback")
    return chosen, (fallback if fallback and fallback not in chosen and exists(fallback) else None)


def _send(sender, channel, out, sleep):
    """-> (None, attempts) when sent, else (why, attempts)."""
    why = None
    for i in range(ATTEMPTS):
        try:
            sender.send(channel, out)
            return None, i + 1
        except DeliveryError as e:
            why = str(e)[:300]
        except Exception as e:  # noqa: BLE001  one broken channel must not stop the others
            why = type(e).__name__
        if i < ATTEMPTS - 1:
            sleep(BACKOFF_S[min(i, len(BACKOFF_S) - 1)])
    return why, ATTEMPTS


def deliver(store, channels, senders, clock, iso, tenant, inv_id, feedback_links, sleep):
    """channels: ports.Channels; senders: kind -> sender; feedback_links: () -> {"right", "wrong"} (made
    only when a finding goes out). -> {"sent": [...], "failed": [...], "fallback": id or None} or a word."""
    inv = store.get(tenant, inv_id)
    if inv is None:
        return "missing"
    if not inv.final:
        return "not finished"
    if inv.status == SKIPPED and not channels.first_skip_notice(tenant, inv.created_at[:13]):
        inv.log(iso(clock.now()), "delivery_skipped", "one rate-limit notice an hour")
        _save(store, inv, {})
        return "quiet"
    rule = channels.rule(tenant, inv.trigger.rule_id) if inv.trigger.rule_id else None
    known = {}

    def exists(cid):
        if cid not in known:
            known[cid] = channels.get(tenant, cid)
        return known[cid] is not None
    chosen, fallback = choose_channels(inv, channels.settings(tenant), rule, exists)
    todo = [c for c in chosen if (inv.deliveries.get(c) or {}).get("status") != "sent"]
    if not chosen:
        inv.log(iso(clock.now()), "delivery_failed", "no investigation channel: set them in Alerts > Channels")
        _save(store, inv, {})
        return {"sent": [], "failed": [], "fallback": None}
    out = outcome(inv, feedback_links() if inv.status == COMPLETED and todo else None)
    done = {}
    for cid in todo:
        done[cid] = _attempt(senders, known[cid], out, sleep, clock, iso)
    any_sent = any(d.get("status") == "sent" for d in [*done.values(), *(inv.deliveries.get(c) or {} for c in chosen)])
    if not any_sent and fallback and (inv.deliveries.get(fallback) or {}).get("status") != "sent":
        done[fallback] = {**_attempt(senders, known[fallback], out, sleep, clock, iso), "fallback": True}
    for cid, d in done.items():
        inv.log(iso(clock.now()), "delivered" if d["status"] == "sent" else "delivery_failed",
                f"{d['kind']} {known[cid].get('name', cid)}{' (fallback)' if d.get('fallback') else ''}"
                f"{': ' + d['error'] if d.get('error') else ''}")
    _save(store, inv, done)
    return {"sent": [c for c, d in done.items() if d["status"] == "sent"], "failed": [c for c, d in done.items() if d["status"] != "sent"],
            "fallback": fallback if fallback in done else None}


def _attempt(senders, channel, out, sleep, clock, iso):
    sender = senders.get(channel["type"])
    if sender is None:
        why, n = f"no adapter for {channel['type']} channels", 0
    else:
        why, n = _send(sender, channel, out, sleep)
    return {"kind": channel["type"], "status": "failed" if why else "sent", "attempts": n, "at": iso(clock.now()),
            **({"error": why} if why else {})}


def _save(store, inv, done):
    """Saves the delivery results; another writer (feedback, a joined trigger) may have saved since."""
    inv.deliveries.update(done)
    for _ in range(5):
        try:
            store.save(inv)
            return
        except StaleWrite:
            fresh = store.get(inv.tenant, inv.id)
            fresh.events += [e for e in inv.events if e not in fresh.events]
            fresh.deliveries.update(done)
            inv = fresh
    raise StaleWrite(f"investigation {inv.id}: delivery results not saved")


def settings_from(body, exists):
    """The tenant's investigation settings from the app -> {channels, services, fallback}. Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")

    def ids(v, what):
        if v is None:
            return []
        if not isinstance(v, list) or len(v) > 20 or not all(isinstance(c, str) for c in v):
            raise ValueError(f"{what}: a list of channel ids")
        for c in v:
            if not exists(c):
                raise ValueError(f"{what}: no channel {c!r}")
        return list(dict.fromkeys(v))
    services = body.get("services") or {}
    if not isinstance(services, dict) or len(services) > 100:
        raise ValueError("services: {service name: [channel ids]}, at most 100")
    out = {"channels": ids(body.get("channels"), "channels"),
           "services": {str(k).strip()[:128]: ids(v, f"services.{k}") for k, v in services.items() if str(k).strip()}}
    fb = body.get("fallback")
    if fb:
        if not isinstance(fb, str) or not exists(fb):
            raise ValueError("fallback: one of your channels")
        out["fallback"] = fb
    return {k: v for k, v in out.items() if v}
