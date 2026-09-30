"""Maintenance windows, excluded runs and SLO settings (reliability.py, through synthetics.api)."""
import json
from datetime import datetime, timezone

import boto3
import pytest
from test_synthetics import aws, call, settings  # noqa: F401  (aws: fixture; sets up the import path)

import reliability as rel  # noqa: E402
import synthetics  # noqa: E402

UTC = timezone.utc


def weekly(days, start, minutes, tz="UTC", checks=("*",)):
    return {"name": "Deploys", "checks": list(checks), "schedule": {"type": "weekly", "days": days, "start": start,
                                                                     "duration_minutes": minutes, "timezone": tz}}


def test_once_window():
    w = rel.validate_window({"name": "Release", "checks": ["*"], "schedule": {"type": "once", "start": "2026-10-01T22:00:00Z",
                                                                                "end": "2026-10-01T23:30:00+00:00"}}, set())
    assert w["schedule"] == {"type": "once", "start": "2026-10-01T22:00:00Z", "end": "2026-10-01T23:30:00Z"}
    assert not rel.is_open(w, datetime(2026, 10, 1, 21, 59, tzinfo=UTC))
    assert rel.is_open(w, datetime(2026, 10, 1, 22, 0, tzinfo=UTC)) and rel.is_open(w, datetime(2026, 10, 1, 23, 29, tzinfo=UTC))
    assert not rel.is_open(w, datetime(2026, 10, 1, 23, 30, tzinfo=UTC))


def test_weekly_window_across_midnight_in_its_time_zone():
    # Tuesdays 23:00-01:00 New York time (EDT, UTC-4 in October).
    w = rel.validate_window(weekly(["tue"], "23:00", 120, "America/New_York"), set())
    at = lambda *a: datetime(*a, tzinfo=UTC)   # noqa: E731
    assert not rel.is_open(w, at(2026, 10, 7, 2, 59))   # Tue 22:59 EDT
    assert rel.is_open(w, at(2026, 10, 7, 3, 0))        # Tue 23:00 EDT
    assert rel.is_open(w, at(2026, 10, 7, 4, 59))       # Wed 00:59 EDT: yesterday's window still open
    assert not rel.is_open(w, at(2026, 10, 7, 5, 0))    # Wed 01:00 EDT
    assert not rel.is_open(w, at(2026, 10, 8, 3, 30))   # Wed 23:30: not a Tuesday
    # After the clocks change (EST, UTC-5) the window follows local time.
    assert rel.is_open(w, at(2026, 11, 11, 4, 0)) and not rel.is_open(w, at(2026, 11, 11, 3, 30))


def test_window_and_slo_validation():
    bad_windows = [({"schedule": {"type": "daily"}}, "once or weekly"),
                   ({"schedule": {"type": "once", "start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"}}, "after the start"),
                   ({"schedule": {"type": "once", "start": "2026-10-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}}, "31 days"),
                   ({"schedule": weekly(["funday"], "22:00", 60)["schedule"]}, "days"),
                   ({"schedule": weekly(["mon"], "25:00", 60)["schedule"]}, "time of day"),
                   ({"schedule": weekly(["mon"], "22:00", 60, "Mars/Olympus")["schedule"]}, "time zone"),
                   ({"checks": ["nope00000000"]}, "no such check")]
    for change, reason in bad_windows:
        with pytest.raises(synthetics.Refused, match=reason):
            rel.validate_window({**weekly(["mon"], "22:00", 60), **change}, {"abc123abc123"})
    slo = {"name": "Checkout", "type": "availability", "checks": ["abc123abc123"], "target": 99.9, "window_days": 30}
    assert rel.validate_slo(slo, {"abc123abc123"})["target"] == 99.9
    for change, reason in [({"target": 100}, "50-99.999"), ({"window_days": 90}, "data is kept 30 days"), ({"type": "fast"}, "availability or"),
                           ({"type": "performance"}, "threshold_ms"), ({"checks": []}, "1-50 checks"), ({"checks": ["*"]}, "no such check")]:
        with pytest.raises(synthetics.Refused, match=reason):
            rel.validate_slo({**slo, **change}, {"abc123abc123"})


def test_windows_and_slos_api_per_tenant(aws):
    _, c = call("acme", "POST", "/v1/app/checks", settings())
    s, w = call("acme", "POST", "/v1/app/windows", weekly(["mon", "thu"], "22:00", 60, checks=[c["id"]]))
    assert s == 201 and w["schedule"]["days"] == ["mon", "thu"] and "tenant" not in w
    assert call("acme", "POST", "/v1/app/windows", weekly(["mon"], "22:00", 60, checks=["abc123abc123"]))[0] == 400
    s, slo = call("acme", "POST", "/v1/app/slos", {"name": "Home up", "type": "availability", "checks": [c["id"]], "target": 99.5, "window_days": 7})
    assert s == 201 and slo["target"] == 99.5
    assert call("globex", "POST", "/v1/app/slos", {**slo, "checks": [c["id"]]})[0] == 400      # not their check
    for path, one in (("/v1/app/windows", w), ("/v1/app/slos", slo)):
        assert [i["id"] for i in call("acme", "GET", path)[1]["items"]] == [one["id"]]
        assert call("globex", "GET", path)[1]["items"] == []
        for method in ("GET", "PUT", "DELETE"):
            assert call("globex", method, path + "/{id}", {}, one["id"])[0] == 404
    s, u = call("acme", "PUT", "/v1/app/slos/{id}", {"target": 99.95}, slo["id"])
    assert s == 200 and u["target"] == 99.95 and u["name"] == "Home up"
    assert call("acme", "DELETE", "/v1/app/windows/{id}", None, w["id"])[0] == 200
    assert call("acme", "GET", "/v1/app/windows")[1]["items"] == []


def test_excluding_runs(aws, monkeypatch):
    _, c = call("acme", "POST", "/v1/app/checks", settings())
    run = "a" * 32
    s, e = call("acme", "POST", "/v1/app/checks/{id}/exclusions", {"run_id": run, "reason": "Deploy 4.2"}, c["id"])
    assert s == 201 and e["by"] == "ana@acme.io"
    assert call("globex", "POST", "/v1/app/checks/{id}/exclusions", {"run_id": run}, c["id"])[0] == 404
    assert call("acme", "POST", "/v1/app/checks/{id}/exclusions", {"run_id": "../x"}, c["id"])[0] == 400
    got = call("acme", "GET", "/v1/app/checks/{id}", None, c["id"])[1]["exclusions"]
    assert [(x["run_id"], x["reason"]) for x in got] == [(run, "Deploy 4.2")]
    assert call("acme", "GET", "/v1/app/checks")[1]["checks"][0]["excluded_runs"] == [run]
    # Undo.
    ev = {"httpMethod": "DELETE", "resource": "/v1/app/checks/{id}/exclusions/{run}", "pathParameters": {"id": c["id"], "run": run},
          "requestContext": {"authorizer": {"claims": {"custom:tenant": "acme", "email": "x@acme.io"}}}}
    assert synthetics.api(ev, None)["statusCode"] == 200
    assert call("acme", "GET", "/v1/app/checks/{id}", None, c["id"])[1]["exclusions"] == []
    # They expire with the data; deleting the check removes them.
    call("acme", "POST", "/v1/app/checks/{id}/exclusions", {"run_id": run}, c["id"])
    monkeypatch.setattr(synthetics, "_now", lambda: "2099-01-01T00:00:00Z")
    assert synthetics.exclusions("acme") == []
    call("acme", "POST", "/v1/app/checks/{id}/exclusions", {"run_id": "b" * 32}, c["id"])
    call("acme", "DELETE", "/v1/app/checks/{id}", None, c["id"])
    left = [i["pk"] for i in boto3.resource("dynamodb").Table("obs-tenants").scan()["Items"]]
    assert not [p for p in left if p.startswith("exclude#")]


def test_runs_in_an_open_window_are_recorded_as_excluded(aws, monkeypatch):
    monkeypatch.setattr(synthetics, "MAX_CHECKS", 50)
    _, a = call("acme", "POST", "/v1/app/checks", settings(name="in window", frequency=1))
    _, b = call("acme", "POST", "/v1/app/checks", settings(name="not in it", frequency=1))
    call("acme", "POST", "/v1/app/windows", {"name": "Deploy", "checks": [a["id"]], "schedule": {
        "type": "once", "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}})
    call("globex", "POST", "/v1/app/checks", settings(name="other tenant", frequency=1))
    invoked = []
    monkeypatch.setattr(synthetics.boto3, "client", lambda name: type("L", (), {
        "invoke": lambda self, **kw: invoked.append(json.loads(kw["Payload"]))})())
    monkeypatch.setattr(synthetics.time, "time", lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC).timestamp())
    assert synthetics.tick({}, None) == {"due": 3}
    marked = {c["name"]: c.get("maintenance") for p in invoked for c in p["checks"]}
    assert marked == {"in window": "Deploy", "not in it": None, "other tenant": None}
    # The runner records it on the trace, metrics and log.
    item = next(c for p in invoked for c in p["checks"] if c["name"] == "in window")
    monkeypatch.setattr(synthetics, "run_any", lambda check, plain, budget=None: {
        "ok": False, "failure": "Home: status 503", "failed_step": 0, "total_ms": 5.0, "tls_days": None, "started": 1.0,
        "steps": [{"name": "Home", "ok": False, "failure": "status 503", "status": 503, "url": "https://example.com/", "timings": {"total_ms": 5.0},
                   "tls_days": None, "extracted": [], "started": 1.0, "body_sample": None}]})
    synthetics.run({"checks": [item]}, None)
    sent = b"".join(r for _, recs in aws for r in recs)
    import gzip
    text = b"".join(gzip.decompress(x) if x[:2] == b"\x1f\x8b" else x for _, recs in aws for x in recs)
    assert text.count(b"maintenance window: Deploy") >= 3 and sent
    assert b"check.run_id" in text
