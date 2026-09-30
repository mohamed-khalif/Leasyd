"""Maintenance windows, excluded runs and SLOs of synthetic checks: settings a customer keeps in the
portal, stored as obs-tenants items (tenant attribute; listed through the by-tenant index).

  window#<tenant>#<id>             a maintenance window: while it is open the tenant's chosen checks
                                   still run, but each run is marked excluded (attribute
                                   check.excluded on its trace, metrics and log), so it doesn't
                                   count against uptime or SLOs and won't alert.
                                     once   {start, end}                        (UTC, at most 31 days)
                                     weekly {days, start "HH:MM", duration_minutes, timezone}
  exclude#<tenant>#<check>#<run>   one run marked excluded afterwards ("false alarm: deployment"),
                                   with who and why. Undo by deleting it. Results are immutable, so
                                   the portal leaves these runs out of uptime, charts and SLOs by
                                   their run id (attribute check.run_id); kept as long as the data
                                   (RETENTION_DAYS + 1), then removed when next listed.
  slo#<tenant>#<id>                an SLO over one or more checks, evaluated from their results:
                                     availability  % of runs that passed
                                     performance   % of runs that took at most threshold_ms
                                   target (e.g. 99.9) over the last window_days (7, 14 or 30).
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from safety import Refused

MAX_WINDOWS, MAX_SLOS, MAX_EXCLUSIONS = 20, 50, 500
KEEP_DAYS = 31                          # an exclusion lasts as long as the runs it refers to
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WINDOW_DAYS = (7, 14, 30)
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_RUN = re.compile(r"^[0-9a-f]{32}$")
_ID = re.compile(r"^[a-z0-9]{12}$")


def _text(v, what, lo, hi):
    s = "" if v is None else str(v).strip()
    if not lo <= len(s) <= hi:
        raise Refused(f"{what}: {lo}-{hi} characters")
    return s


def _time(v, what):
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise Refused(f"{what}: a date and time such as 2026-10-01T22:00:00Z")
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _checks(v, known, allow_all):
    ids = list(v or [])
    if allow_all and ids == ["*"]:
        return ids
    if not ids or len(ids) > 50:
        raise Refused("checks: choose 1-50 checks" + (" (or all)" if allow_all else ""))
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise Refused(f"checks: no such check {unknown[0]!r}")
    return sorted(set(ids))


# ------------------------------------------------------------------ maintenance windows

def validate_window(body, known_checks):
    if not isinstance(body, dict):
        raise Refused("expected a JSON object")
    s = body.get("schedule") or {}
    kind = s.get("type")
    if kind == "once":
        start, end = _time(s.get("start"), "start"), _time(s.get("end"), "end")
        if not start < end <= start + timedelta(days=31):
            raise Refused("end: after the start, and at most 31 days later")
        schedule = {"type": "once", "start": _iso(start), "end": _iso(end)}
    elif kind == "weekly":
        days = [d for d in DAYS if d in (s.get("days") or [])]
        if not days or len(days) != len(set(s.get("days") or [])):
            raise Refused(f"days: one or more of {', '.join(DAYS)}")
        if not _HHMM.match(str(s.get("start") or "")):
            raise Refused("start: a time of day such as 22:00")
        minutes = int(s.get("duration_minutes") or 0)
        if not 1 <= minutes <= 24 * 60:
            raise Refused("duration_minutes: 1-1440")
        tz = str(s.get("timezone") or "UTC")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise Refused(f"timezone: {tz!r} is not a time zone such as Europe/London or America/New_York")
        schedule = {"type": "weekly", "days": days, "start": s["start"], "duration_minutes": minutes, "timezone": tz}
    else:
        raise Refused("schedule type: once or weekly")
    return {"name": _text(body.get("name"), "name", 1, 80), "checks": _checks(body.get("checks"), known_checks, True),
            "schedule": schedule}


def is_open(window, now):
    """Whether a window is open at `now` (an aware datetime)."""
    s = window["schedule"]
    if s["type"] == "once":
        return _time(s["start"], "start") <= now < _time(s["end"], "end")
    tz = ZoneInfo(s["timezone"])
    local = now.astimezone(tz)
    hh, mm = map(int, s["start"].split(":"))
    for back in (0, 1):                 # today's opening, or yesterday's still open past midnight
        day = (local - timedelta(days=back)).date()
        if DAYS[day.weekday()] not in s["days"]:
            continue
        opens = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        if opens <= local < opens + timedelta(minutes=int(s["duration_minutes"])):
            return True
    return False


def open_window(windows, check_id, now):
    """The name of an open window covering the check, or None."""
    for w in sorted(windows, key=lambda w: w["name"]):
        if (w["checks"] == ["*"] or check_id in w["checks"]) and is_open(w, now):
            return w["name"]
    return None


# ------------------------------------------------------------------ excluded runs

def validate_exclusion(body):
    run_id = str((body or {}).get("run_id") or "")
    if not _RUN.match(run_id):
        raise Refused("run_id: the run's trace id (32 hex characters)")
    return {"run_id": run_id, "reason": _text(body.get("reason") or "Excluded", "reason", 1, 200)}


def exclusion_expires(now):
    return _iso(now + timedelta(days=KEEP_DAYS))


# ------------------------------------------------------------------ SLOs

def validate_slo(body, known_checks):
    if not isinstance(body, dict):
        raise Refused("expected a JSON object")
    kind = body.get("type")
    if kind not in ("availability", "performance"):
        raise Refused("type: availability or performance")
    try:
        target = round(float(body.get("target")), 3)
    except (TypeError, ValueError):
        raise Refused("target: a percentage such as 99.9")
    if not 50 <= target < 100:
        raise Refused("target: 50-99.999 (%)")
    window = int(body.get("window_days") or 30)
    if window not in WINDOW_DAYS:
        raise Refused(f"window_days: {', '.join(map(str, WINDOW_DAYS))} (data is kept 30 days)")
    out = {"name": _text(body.get("name"), "name", 1, 80), "description": _text(body.get("description"), "description", 0, 500),
           "type": kind, "checks": _checks(body.get("checks"), known_checks, False), "target": target, "window_days": window}
    if kind == "performance":
        ms = int(body.get("threshold_ms") or 0)
        if not 1 <= ms <= 60_000:
            raise Refused("threshold_ms: 1-60000")
        out["threshold_ms"] = ms
    return out


def _iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_id(v):
    return bool(_ID.match(v or ""))


def is_run(v):
    return bool(_RUN.match(v or ""))
