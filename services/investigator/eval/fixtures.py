"""Recorded-style telemetry for eval cases: a small pool of logs, spans, traces and metric series per
incident, answered through the same AI SRE tool names and arguments the real adapter uses
(adapters/claude.py SreTelemetry), so the agent's tools run unchanged on top of it.

Pools are filtered the way obs-query would (service, severity, text, errors, attributes) and
top_values groups them, each item counting `n` times. The per-minute summaries the change tools read
(svc_1m, edge_1m) come from the case's `versions` (default: each service on one version all along)
and `edges`; recorded deploys from its `deploys` (this object is the agent's Deploys port too).
Items marked key=True are the ones that show the root cause: the scorer checks the finding cites
evidence that contained one.
"""

import json
import re

SEVERITY = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
HIDDEN = ("key", "n")


def _clean(item):
    return {k: v for k, v in item.items() if k not in HIDDEN}


def _field(item, name):
    name = name.removeprefix("attributes.").removeprefix("resource.")
    if name in ("severity_number",):
        return SEVERITY.get(str(item.get("severity", "")).upper(), 0)
    if name == "severity_text":
        return item.get("severity")
    if name == "status_code":
        return 2 if item.get("status") == "error" else 0
    if name == "duration_ns":
        return float(item.get("duration_ms") or 0) * 1e6
    if name in item:
        return item[name]
    return (item.get("attributes") or {}).get(name)


def _holds(item, w):
    v, want, op = _field(item, w.get("field", "")), w.get("value"), w.get("op", "=")
    if op == "exists":
        return v is not None
    if v is None:
        return False
    if op == "contains":
        return str(want).lower() in str(v).lower()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            want = float(want)
        except (TypeError, ValueError):
            return False
    else:
        v, want = str(v).lower(), str(want).lower()
    return {"=": v == want, "!=": v != want, "<": v < want, "<=": v <= want, ">": v > want, ">=": v >= want}[op]


def _pctl(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


class FixtureTelemetry:
    """One case's telemetry. `log` records each call: the tool, its arguments, whether the answer held
    data and whether it held a key item (the n-th call is the agent's evidence ev<n>)."""

    def __init__(self, case):
        self.case, self.log, self.by_evidence, self._marked = case, [], {}, 0

    def run(self, name, args):
        out, items = getattr(self, "_" + name)(args)
        self._note(name, items)
        return out

    def _note(self, name, items):
        self.log.append({"tool": name, "data": bool(items), "key": any(i.get("key") for i in items)})

    def evidence_made(self, ev_id):
        """The agent's tools made evidence ev_id from the calls since the last one."""
        got = self.log[self._marked:]
        self._marked = len(self.log)
        self.by_evidence[ev_id] = {"data": any(c["data"] for c in got), "key": any(c["key"] for c in got)}

    # -- the Deploys port

    def between(self, start, end, service=None):
        hits = [d for d in self.case.get("deploys") or [] if start <= d["deployed_at"] <= end
                and (not service or d["service"].lower() == service.lower())]
        self._note("deploys", hits)
        return [_clean(d) for d in sorted(hits, key=lambda d: d["deployed_at"])]

    # -- the summaries, as SQL answers

    def _versions(self):
        if "versions" in self.case:
            return self.case["versions"]
        return [{"service": s["service"], "version": "1.0.0", "first_seen": "2026-10-20T00:00:00Z", "last_seen": "2026-10-30T02:44:00Z",
                 "requests": s.get("spans", 0), "errors": round(s.get("spans", 0) * (s.get("error_pct") or 0) / 100)}
                for s in self.case.get("services") or [] if s.get("spans")]

    def _run_sql(self, a):
        sql, start, end = a["sql"], a.get("start", ""), a.get("end", "")
        svc = re.search(r"service = '((?:[^']|'')*)'", sql)
        svc = svc.group(1).replace("''", "'").lower() if svc else None
        if "from edge_1m" in sql.lower():
            hits = [e for e in self.case.get("edges") or [] if svc in (e["source"].lower(), e["target"].lower())]
            return {"columns": ["source", "target", "calls", "errors"], "rows": [[e["source"], e["target"], e["calls"], e["errors"]] for e in hits]}, hits
        hits = [v for v in self._versions() if v["first_seen"] < end and v["last_seen"] >= start and (not svc or v["service"].lower() == svc)]
        cols = ["service", "version", "first_seen", "last_seen", "requests", "errors"]
        rows = [[v["service"], v["version"], max(v["first_seen"], start), min(v["last_seen"], end), v["requests"], v["errors"]] for v in hits]
        return {"columns": cols, "rows": sorted(rows, key=lambda r: (r[0], r[2]))}, hits

    def _rng(self, a):
        return {"range": [a.get("start"), a.get("end")]} if a.get("start") else {}

    def _list_services(self, a):
        rows = self.case.get("services") or []
        return {**self._rng(a), "services": [_clean(r) for r in rows]}, rows

    def _query_promql(self, a):
        text = str(a.get("promql", "")).lower()
        for rule in self.case.get("metrics") or []:
            if all(s.lower() in text for s in rule["match"]):
                return {**self._rng(a), "step_seconds": int(a.get("step_seconds") or 60), "series": rule["series"]}, [rule]
        return {**self._rng(a), "step_seconds": int(a.get("step_seconds") or 60), "series": []}, []

    def _filter(self, pool, a):
        out = []
        for i in pool:
            if a.get("service") and str(i.get("service", "")).lower() != str(a["service"]).lower():
                continue
            if a.get("min_severity") and SEVERITY.get(str(i.get("severity", "")).upper(), 0) < SEVERITY[a["min_severity"]]:
                continue
            if a.get("text") and str(a["text"]).lower() not in str(i.get("body", "")).lower():
                continue
            if a.get("trace_id") and i.get("trace_id") != a["trace_id"]:
                continue
            if a.get("span_name") and str(i.get("name", "")).lower() != str(a["span_name"]).lower():
                continue
            if a.get("errors_only") and i.get("status") != "error":
                continue
            if a.get("min_duration_ms") is not None and float(i.get("duration_ms") or 0) < float(a["min_duration_ms"]):
                continue
            if any(str(_field(i, k)) != str(v) for k, v in (a.get("attributes") or {}).items()):
                continue
            out.append(i)
        return sorted(out, key=lambda i: i.get("ts", ""), reverse=True)[: min(int(a.get("limit") or 20), 50)]

    def _search_logs(self, a):
        hits = self._filter(self.case.get("logs") or [], a)
        return {**self._rng(a), "records": [_clean(i) for i in hits]}, hits

    def _search_spans(self, a):
        hits = self._filter(self.case.get("spans") or [], a)
        return {**self._rng(a), "spans": [_clean(i) for i in hits]}, hits

    def _get_trace(self, a):
        t = (self.case.get("traces") or {}).get(str(a.get("trace_id", "")).lower())
        if not t:
            return {"trace_id": a.get("trace_id"), "found": False}, []
        return {"trace_id": a["trace_id"], **{k: v for k, v in t.items() if k != "key"}}, [t]

    def _top_values(self, a):
        pool = self.case.get("logs" if a.get("signal") == "logs" else "spans") or []
        items = [i for i in pool if all(_holds(i, w) for w in a.get("where") or [])]
        group_by = list(a.get("group_by") or [])[:4]
        measure = a.get("measure") or "count"
        groups = {}
        for i in items:
            groups.setdefault(tuple(json.dumps(_field(i, g), default=str) for g in group_by), []).append(i)
        rows = []
        for k, members in groups.items():
            count = sum(int(m.get("n", 1)) for m in members)
            if measure == "count":
                value = [count]
            else:
                fn, field = measure.split(":", 1)
                xs = [float(_field(m, field) or 0) for m in members for _ in range(min(int(m.get("n", 1)), 50))]
                v = {"avg": sum(xs) / len(xs), "max": max(xs), "min": min(xs), "sum": sum(xs)}.get(fn)
                v = v if v is not None else _pctl(xs, float(fn.lstrip("p") or 50))
                value = [round(v, 1), count]
            rows.append(([json.loads(x) for x in k] + value, members))
        rows.sort(key=lambda r: -r[0][len(group_by)])
        rows = rows[: min(int(a.get("limit") or 15), 50)]
        cols = group_by + ([measure] if measure == "count" else [measure, "count"])
        return {**self._rng(a), "columns": cols, "rows": [r for r, _ in rows]}, [m for _, ms in rows for m in ms]
