"""Deploys: what changed and when, so an investigation can line changes up with the incident.

Two sources:
  recorded   the team's CI posts each deploy (POST /v1/deploys, with an ingest API key)
  inferred   a service's version (service.version on its telemetry) first seen at a time; for teams
             that don't send deploys, and to catch deploys CI didn't report

parse() checks a posted deploy; version_changes() turns per-version first/last seen rows into changes.
"""

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

STATUSES = ("succeeded", "started", "failed", "rolled_back")
MAX_AGE = timedelta(days=7)        # a deploy posted late (a backfill) may be this old
MAX_AHEAD = timedelta(minutes=5)   # clock skew
KEEP_DAYS = 90
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LIMITS = {"service": 128, "version": 128, "environment": 64, "commit": 64, "repository": 200, "url": 500, "description": 500}


@dataclass
class Deploy:
    id: str
    service: str
    version: str
    deployed_at: str               # ISO-8601 UTC, seconds
    environment: str = ""
    status: str = "succeeded"
    commit: str = ""
    repository: str = ""
    url: str = ""
    description: str = ""

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v not in ("", None)}


def _iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(body, now):
    """A posted deploy -> Deploy. Raises ValueError with what to fix. now: an aware datetime."""
    if not isinstance(body, dict):
        raise ValueError("body: a JSON object")
    f = {}
    for k, n in _LIMITS.items():
        v = body.get(k)
        if v is None or v == "":
            continue
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            raise ValueError(f"{k}: text")
        v = str(v).strip()
        if len(v) > n:
            raise ValueError(f"{k}: at most {n} characters")
        f[k] = v
    for k in ("service", "version"):
        if not f.get(k):
            raise ValueError(f"{k} is required")
    if f.get("url") and not f["url"].startswith("https://"):
        raise ValueError("url: an https:// link (e.g. the CI run)")
    status = str(body.get("status") or "succeeded")
    if status not in STATUSES:
        raise ValueError(f"status: one of {', '.join(STATUSES)}")
    when = now
    if body.get("deployed_at"):
        try:
            when = datetime.fromisoformat(str(body["deployed_at"]).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("deployed_at: ISO-8601, e.g. 2026-10-30T02:12:00Z")
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        if not now - MAX_AGE <= when <= now + MAX_AHEAD:
            raise ValueError("deployed_at: within the last 7 days")
    at = _iso(when)
    given = body.get("id")
    if given is not None and not (isinstance(given, str) and _ID.match(given)):
        raise ValueError("id: up to 128 letters, digits and . _ : -")
    dep_id = given or hashlib.sha256("|".join([f["service"], f["version"], f.get("environment", ""), at]).encode()).hexdigest()[:16]
    return Deploy(id=dep_id, deployed_at=at, status=status, **f)


def version_changes(rows, since):
    """Rows {service, version, first_seen, last_seen, requests, errors} (one per service and version,
    from the per-minute summaries) -> the versions that appeared after `since` (ISO), each with the
    version it replaced. A service's first version in the range isn't a change: it may have run for weeks."""
    by_service = {}
    for r in rows:
        by_service.setdefault(r["service"], []).append(r)
    out = []
    for svc, vs in by_service.items():
        vs.sort(key=lambda r: r["first_seen"])
        for prev, cur in zip(vs, vs[1:]):
            if cur["first_seen"] > since:
                out.append({"service": svc, "version": cur["version"], "first_seen": cur["first_seen"], "previous": prev["version"],
                            "previous_last_seen": prev["last_seen"], "source": "inferred from service.version"})
    return sorted(out, key=lambda c: c["first_seen"])
