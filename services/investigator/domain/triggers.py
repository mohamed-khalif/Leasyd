"""Payloads that can start an investigation, normalised to a Trigger. Pure functions, no I/O.

  from_alert(payload)          a Leasyd alert firing (alerts.message()'s payload, sent by obs-alerts)
  from_webhook(body)           the public webhook's body, whichever of these it is:
      CloudWatch alarm via SNS   {"Type": "Notification", "Message": "<alarm JSON>"}; the SNS
                                 subscription's confirmation ({"Type": "SubscriptionConfirmation"}) too
      PagerDuty (webhooks v3)    {"event": {"event_type": "incident.triggered", "data": {...}}}
      generic                    {"title": ..., "service"?, "severity"?, "dedupe_key"?, "started_at"?,
                                  "url"?, "details"?}

Each returns one of
  ("trigger", Trigger)        investigate this
  ("ignore", why)             a resolution, an acknowledgement, an OK state: nothing to investigate
  ("confirm", url)            SNS wants its subscription confirmed at url (only an AWS SNS https URL)
and raises ValueError for a body it can't read.
"""

import json
import re
from datetime import datetime, timezone

from domain.model import Trigger

_SNS_URL = re.compile(r"^https://sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?/\?")
_SEVERITIES = {"critical", "warning", "info"}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(ts):
    """Any ISO-8601 time (or CloudWatch's +0000 form) -> UTC "...Z"; None/unreadable -> now."""
    if not ts:
        return _now_iso()
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return _now_iso()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_alert(p):
    if p.get("state") != "firing":
        return "ignore", f"alert state {p.get('state')}"
    rule = p.get("rule") or {}
    if not rule.get("id"):
        raise ValueError("alert: rule.id missing")
    subject = str(p.get("subject") or "")
    return "trigger", Trigger(
        source="alert", title=f"{rule.get('name', 'Alert')}: {subject}" if subject else str(rule.get("name", "Alert")),
        dedupe_key=f"alert:{rule['id']}:{subject}", started_at=_iso(p.get("time")), subject=subject,
        rule_id=rule["id"], url=p.get("url"), details=str(p.get("detail") or ""))


def from_webhook(body):
    if not isinstance(body, dict):
        raise ValueError("body: a JSON object")
    if body.get("Type") == "SubscriptionConfirmation":
        url = str(body.get("SubscribeURL") or "")
        if not _SNS_URL.match(url):
            raise ValueError("SubscribeURL: not an AWS SNS address")
        return "confirm", url
    if body.get("Type") == "Notification":
        return _cloudwatch(body)
    if isinstance(body.get("event"), dict) and "event_type" in body["event"]:
        return _pagerduty(body["event"])
    return _generic(body)


def _cloudwatch(note):
    try:
        alarm = json.loads(note.get("Message") or "")
    except ValueError:
        raise ValueError("SNS Message: not a CloudWatch alarm")
    if not isinstance(alarm, dict) or "AlarmName" not in alarm:
        raise ValueError("SNS Message: not a CloudWatch alarm")
    if alarm.get("NewStateValue") != "ALARM":
        return "ignore", f"alarm state {alarm.get('NewStateValue')}"
    trig = alarm.get("Trigger") or {}
    dims = {d.get("name") or d.get("Name"): d.get("value") or d.get("Value") for d in trig.get("Dimensions") or []}
    service = dims.get("ServiceName") or dims.get("FunctionName") or dims.get("service")
    metric = f"{trig.get('Namespace', '')} {trig.get('MetricName', '')}".strip()
    return "trigger", Trigger(
        source="cloudwatch", title=f"CloudWatch alarm: {alarm['AlarmName']}",
        dedupe_key=f"cloudwatch:{alarm.get('AlarmArn') or alarm['AlarmName']}", started_at=_iso(alarm.get("StateChangeTime")),
        subject=alarm["AlarmName"], service=service, details=f"{alarm.get('NewStateReason', '')} Metric: {metric}. "
        f"Dimensions: {json.dumps(dims)}")


def _pagerduty(ev):
    if ev.get("event_type") != "incident.triggered":
        return "ignore", f"PagerDuty {ev.get('event_type')}"
    d = ev.get("data") or {}
    if not d.get("id"):
        raise ValueError("PagerDuty: data.id missing")
    service = (d.get("service") or {}).get("summary")
    return "trigger", Trigger(
        source="pagerduty", title=str(d.get("title") or "PagerDuty incident"), dedupe_key=f"pagerduty:{d['id']}",
        started_at=_iso(ev.get("occurred_at")), subject=service or str(d.get("title") or ""), service=service,
        severity="critical" if d.get("urgency") == "high" else "warning", url=d.get("html_url"),
        details=str(d.get("description") or d.get("title") or ""))


def _generic(b):
    title = str(b.get("title") or "").strip()
    if not title:
        raise ValueError("title: what is wrong (required)")
    severity = str(b.get("severity") or "critical").lower()
    if severity not in _SEVERITIES:
        raise ValueError(f"severity: one of {sorted(_SEVERITIES)}")
    service = str(b["service"]).strip()[:120] if b.get("service") else None
    return "trigger", Trigger(
        source="generic", title=title, dedupe_key=f"generic:{str(b.get('dedupe_key') or f'{service}:{title}')[:300]}",
        started_at=_iso(b.get("started_at")), subject=service or title, service=service, severity=severity,
        url=str(b["url"])[:500] if b.get("url") else None, details=str(b.get("details") or ""))
