"""The investigation's read-only tools over the tenant's telemetry. Each call becomes an Evidence
record (id ev1, ev2..., what was asked, the window, a short summary, a link to the same data in the
Leasyd app) and the model gets the answer back as data, redacted and cut to size, tagged with that id
so the finding can cite it.

  list_services    every service: requests/s, error %, p95 (a first look)
  query_metrics    PromQL over metrics, spans and logs as series
  query_logs       log records (search) or log groups (top: most frequent messages, by service...)
  query_traces     spans (search), groups of spans (top), or one whole trace (trace)
  get_recent_changes   deploys the team recorded (CI -> POST /v1/deploys) and versions first seen in the
                   telemetry (service.version, from the per-minute summaries); by default the day before
                   the window to its end
  get_service_context  one service: its versions (requests and errors each), runtime and platform, what it
                   calls and what calls it, its recorded deploys of the last week

Times default to the investigation's window (around when the alert started) and must stay within
MAX_SPAN of it, so the agent looks at the incident, not at a month of data.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from agent.redact import redact
from domain.deploys import version_changes
from domain.model import Evidence

RESULT_CHARS = 12_000
MAX_SPAN = timedelta(hours=24)
CHANGES_BEFORE = timedelta(hours=24)     # get_recent_changes looks this far before the window by default
CONTEXT_DEPLOYS = timedelta(days=7)

# The per-minute summaries (services/compaction/compact.py SUMMARIES svc_1m, edge_1m), as the APM page reads them.
VERSIONS_SQL = ("SELECT service, version, strftime(min(ts), '%Y-%m-%dT%H:%M:%SZ') AS first_seen, "
                "strftime(max(ts), '%Y-%m-%dT%H:%M:%SZ') AS last_seen, sum(requests) AS requests, sum(errors) AS errors, "
                "any_value(language) AS language, any_value(runtime) AS runtime, any_value(platform) AS platform, "
                "any_value(k8s_deployment) AS k8s_deployment, any_value(region) AS region "
                "FROM svc_1m WHERE version IS NOT NULL AND version <> ''{where} GROUP BY 1, 2 ORDER BY 1, 3 LIMIT 300")
EDGES_SQL = ("SELECT source, target, sum(calls) AS calls, sum(errors) AS errors FROM edge_1m "
             "WHERE source = {svc} OR target = {svc} GROUP BY 1, 2 ORDER BY calls DESC LIMIT 30")

_RANGE = {"start": {"type": "string", "description": "ISO-8601 UTC; default: the investigation's window"},
          "end": {"type": "string", "description": "ISO-8601 UTC; default: the investigation's window"}}
_WHERE = {"type": "array", "description": "conditions, e.g. {field: service, op: =, value: checkout}", "items": {
    "type": "object", "properties": {"field": {"type": "string"}, "op": {"type": "string", "enum": ["=", "!=", "<", "<=", ">", ">=", "contains", "exists"]},
                                     "value": {"type": ["string", "number"]}}, "required": ["field", "op"], "additionalProperties": False}}

SPECS = [
    {"name": "list_services", "description": "Every service that sent spans in the window: requests per second, error percentage and p95 duration (ms). A good first step.",
     "input_schema": {"type": "object", "properties": {**_RANGE}, "additionalProperties": False}},
    {"name": "query_metrics", "description": "PromQL over metrics, spans and logs, as a series per label set (min, max, average, first, last and sampled points). leasyd.spans{service_name, span_name, span_kind, status_code}, leasyd.span.duration (seconds, histogram), leasyd.logs{service_name, severity_range}, and every metric by name. Use rate()/increase() for counts.",
     "input_schema": {"type": "object", "properties": {"promql": {"type": "string"}, "step_seconds": {"type": "integer", "description": "multiple of 10"}, **_RANGE},
                      "required": ["promql"], "additionalProperties": False}},
    {"name": "query_logs", "description": "Logs. mode search: the newest records matching service, min_severity, text (substring of the body), attributes, trace_id. mode top: groups and counts, e.g. group_by [body] with min_severity ERROR for the most frequent errors, or [service].",
     "input_schema": {"type": "object", "properties": {
         "mode": {"type": "string", "enum": ["search", "top"]}, "service": {"type": "string"},
         "min_severity": {"type": "string", "enum": ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]}, "text": {"type": "string"},
         "attributes": {"type": "object", "additionalProperties": {"type": "string"}}, "trace_id": {"type": "string"},
         "group_by": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "description": "at most 50"}, **_RANGE},
         "required": ["mode"], "additionalProperties": False}},
    {"name": "get_recent_changes", "description": "What changed: deploys the team recorded from CI, and service versions first seen in the telemetry (service.version), each with the version it replaced. Default range: the day before the investigation's window to its end. An empty answer means no deploy was recorded and no version change was seen; say so rather than assume a change.",
     "input_schema": {"type": "object", "properties": {"service": {"type": "string", "description": "only this service; default all"}, **_RANGE}, "additionalProperties": False}},
    {"name": "get_service_context", "description": "One service: the versions it ran in the range (requests and errors for each), its language, runtime and platform, the services it calls and that call it (calls and errors per connection), and its deploys recorded in the last 7 days.",
     "input_schema": {"type": "object", "properties": {"service": {"type": "string"}, **_RANGE}, "required": ["service"], "additionalProperties": False}},
    {"name": "query_traces", "description": "Traces. mode search: the newest spans matching service, span_name, errors_only, min_duration_ms, attributes. mode top: groups of spans with a measure, e.g. group_by [service, name] measure p95:duration_ns, or [status_message] with where status_code = 2. mode trace: one whole trace (trace_id): its spans as a tree with events and its logs.",
     "input_schema": {"type": "object", "properties": {
         "mode": {"type": "string", "enum": ["search", "top", "trace"]}, "service": {"type": "string"}, "span_name": {"type": "string"},
         "errors_only": {"type": "boolean"}, "min_duration_ms": {"type": "number"}, "attributes": {"type": "object", "additionalProperties": {"type": "string"}},
         "trace_id": {"type": "string"}, "group_by": {"type": "array", "items": {"type": "string"}},
         "measure": {"type": "string", "description": "count (default) or fn:field, e.g. p95:duration_ns"}, "where": _WHERE,
         "limit": {"type": "integer", "description": "at most 50"}, **_RANGE},
         "required": ["mode"], "additionalProperties": False}},
]
NAMES = {s["name"] for s in SPECS}


def _parse(ts):
    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ToolRunner:
    """The tools for one investigation: one tenant's telemetry (bound already), one window."""

    def __init__(self, telemetry, start, end, app_url, clock=time.monotonic, deploys=None):
        self.telemetry, self.window, self.app_url, self.clock = telemetry, (start, end), app_url.rstrip("/"), clock
        self.deploys = deploys          # the tenant's recorded deploys (ports.Deploys), or None
        self.evidence, self.calls, self.redactions = {}, [], {}

    def _range(self, a, before=timedelta(0)):
        start = _parse(a["start"]) if a.get("start") else self.window[0] - before
        end = _parse(a["end"]) if a.get("end") else self.window[1]
        if end <= start:
            raise ValueError("end must be after start")
        if start < self.window[0] - MAX_SPAN or end > self.window[1] + MAX_SPAN:
            raise ValueError("stay within a day of the incident's window")
        return start, end

    def run(self, name, args):
        """-> (text for the model, Evidence or None). Errors come back as text, so the model can correct."""
        t0 = self.clock()
        try:
            if name not in NAMES:
                raise ValueError(f"unknown tool {name}")
            start, end = self._range(args, CHANGES_BEFORE if name == "get_recent_changes" else timedelta(0))
            got = getattr(self, "_" + name)(args, start, end)
            if callable(got[0]):            # a tool of several queries: (fetch, title, link)
                fetch, title, link = got
                out = fetch()
            else:
                sre_name, sre_args, title, link = got
                out = self.telemetry.run(sre_name, {**sre_args, "start": _iso(start), "end": _iso(end)} if sre_name != "get_trace" else sre_args)
        except Exception as e:  # noqa: BLE001  shown to the model, logged on the call
            self.calls.append({"name": name, "input": args, "ms": round((self.clock() - t0) * 1000), "error": str(e)[:300]})
            return f"Error: {str(e)[:500]}", None
        kind = {"list_services": "services", "get_recent_changes": "changes", "get_service_context": "service"}.get(name) or name.replace("query_", "")
        ev = Evidence(id=f"ev{len(self.evidence) + 1}", kind=kind, title=title, summary=_summary(out), link=link,
                      query={"tool": name, **args}, start=_iso(start), end=_iso(end))
        self.evidence[ev.id] = ev
        made = getattr(self.telemetry, "evidence_made", None)      # evals: which data went into which evidence
        if made:
            made(ev.id)
        text, found = redact(json.dumps(out, default=str))
        for k, n in found.items():
            self.redactions[k] = self.redactions.get(k, 0) + n
        if len(text) > RESULT_CHARS:
            text = text[:RESULT_CHARS] + f"... [cut at {RESULT_CHARS} characters; narrow the query]"
        self.calls.append({"name": name, "input": args, "ms": round((self.clock() - t0) * 1000), "evidence": ev.id,
                           "chars": len(text), **({"redacted": found} if found else {})})
        return f"<evidence id=\"{ev.id}\">\n{text}\n</evidence>", ev

    # -- each tool: (AI SRE tool, its arguments, evidence title, link into the app)

    def _rng(self, start, end):
        return f"range=c{int(start.timestamp() * 1000)}-{int(end.timestamp() * 1000)}"

    def _list_services(self, a, start, end):
        return "list_services", {}, "Services: requests, errors, p95", f"{self.app_url}/#/apm?{self._rng(start, end)}"

    def _query_metrics(self, a, start, end):
        args = {"promql": str(a["promql"]), **({"step_seconds": int(a["step_seconds"])} if a.get("step_seconds") else {})}
        return "query_promql", args, f"PromQL: {_cut(a['promql'], 120)}", f"{self.app_url}/#/query?promql={quote(a['promql'])}&{self._rng(start, end)}"

    def _query_logs(self, a, start, end):
        svc = f"service={quote(a['service'])}&" if a.get("service") else ""
        link = f"{self.app_url}/#/logs?{svc}{'q=' + quote(a['text']) + '&' if a.get('text') else ''}{self._rng(start, end)}"
        if a["mode"] == "top":
            where = [{"field": "service", "op": "=", "value": a["service"]}] if a.get("service") else []
            if a.get("min_severity"):
                floor = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}[a["min_severity"]]
                where.append({"field": "severity_number", "op": ">=", "value": floor})
            if a.get("text"):
                where.append({"field": "body", "op": "contains", "value": a["text"]})
            return ("top_values", {"signal": "logs", "group_by": a.get("group_by") or ["body"], "where": where, "limit": min(int(a.get("limit") or 15), 50)},
                    f"Top logs by {', '.join(a.get('group_by') or ['body'])}" + (f" ({a['service']})" if a.get("service") else ""), link)
        keep = {k: a[k] for k in ("service", "min_severity", "text", "attributes", "trace_id", "limit") if a.get(k) is not None}
        return "search_logs", keep, "Logs" + (f" of {a['service']}" if a.get("service") else "") + (f" matching \"{_cut(a['text'], 60)}\"" if a.get("text") else ""), link

    def _sql(self, sql, start, end):
        out = self.telemetry.run("run_sql", {"sql": sql, "start": _iso(start), "end": _iso(end)})
        return [dict(zip(out.get("columns") or [], r)) for r in out.get("rows") or []]

    def _recorded(self, start, end, service):
        if not self.deploys:
            return []
        return self.deploys.between(_iso(start), _iso(end), service or None)

    def _get_recent_changes(self, a, start, end):
        svc = str(a.get("service") or "").strip()

        def fetch():
            recorded = self._recorded(start, end, svc)
            rows = self._sql(VERSIONS_SQL.format(where=f" AND service = {_lit(svc)}" if svc else ""), start, end)
            changes = version_changes(rows, _iso(start + timedelta(minutes=2)))
            out = {"range": [_iso(start), _iso(end)], "recorded_deploys": recorded, "version_changes": changes}
            if not recorded and not changes:
                out["note"] = (f"No deploy recorded and no version change seen{' for ' + svc if svc else ''} in this range "
                               f"({len(rows)} service versions seen, none new).")
            return out
        return (fetch, "Changes: deploys and versions" + (f" ({svc})" if svc else ""),
                f"{self.app_url}/#/apm{'/' + quote(svc, safe='') if svc else ''}?{self._rng(start, end)}")

    def _get_service_context(self, a, start, end):
        svc = str(a.get("service") or "").strip()
        if not svc:
            raise ValueError("service is required")

        def fetch():
            versions = self._sql(VERSIONS_SQL.format(where=f" AND service = {_lit(svc)}"), start, end)
            edges = self._sql(EDGES_SQL.format(svc=_lit(svc)), start, end)
            week = self._recorded(min(start, end - CONTEXT_DEPLOYS), end, svc)
            return {"service": svc, "range": [_iso(start), _iso(end)],
                    "versions": [{k: v for k, v in r.items() if k != "service" and v not in (None, "")} for r in versions],
                    "calls": [{"to": e["target"], "calls": e["calls"], "errors": e["errors"]} for e in edges if e["source"] == svc],
                    "called_by": [{"from": e["source"], "calls": e["calls"], "errors": e["errors"]} for e in edges if e["target"] == svc],
                    "recorded_deploys": week[-10:]}
        return fetch, f"Service {svc}: versions, dependencies, deploys", f"{self.app_url}/#/apm/{quote(svc, safe='')}?{self._rng(start, end)}"

    def _query_traces(self, a, start, end):
        if a["mode"] == "trace":
            if not a.get("trace_id"):
                raise ValueError("mode trace needs trace_id")
            return "get_trace", {"trace_id": a["trace_id"]}, f"Trace {a['trace_id']}", f"{self.app_url}/#/traces/{quote(a['trace_id'])}"
        link = f"{self.app_url}/#/traces?{'service=' + quote(a['service']) + '&' if a.get('service') else ''}{self._rng(start, end)}"
        if a["mode"] == "top":
            where = list(a.get("where") or [])
            if a.get("service"):
                where.append({"field": "service", "op": "=", "value": a["service"]})
            return ("top_values", {"signal": "traces", "group_by": a.get("group_by") or ["service", "name"], "measure": a.get("measure") or "count",
                                   "where": where, "limit": min(int(a.get("limit") or 15), 50)},
                    f"Spans by {', '.join(a.get('group_by') or ['service', 'name'])} ({a.get('measure') or 'count'})", link)
        keep = {k: a[k] for k in ("service", "span_name", "errors_only", "min_duration_ms", "attributes", "limit") if a.get(k) is not None}
        return "search_spans", keep, "Spans" + (f" of {a['service']}" if a.get("service") else "") + (" with errors" if a.get("errors_only") else ""), link


def _lit(v):
    """A SQL string literal (the query engine also refuses anything but one read-only SELECT)."""
    return "'" + str(v).replace("'", "''") + "'"


def _cut(v, n):
    s = str(v or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _summary(out):
    if not isinstance(out, dict):
        return ""
    if "version_changes" in out:
        return f"{len(out['recorded_deploys'])} recorded deploys, {len(out['version_changes'])} version changes"
    if "called_by" in out:
        return f"{len(out['versions'])} versions, calls {len(out['calls'])}, called by {len(out['called_by'])}"
    for k, what in (("services", "services"), ("series", "series"), ("records", "log records"), ("spans", "spans"),
                    ("rows", "rows"), ("groups", "groups"), ("tree", "spans in the trace")):
        if isinstance(out.get(k), list):
            return f"{len(out[k])} {what}"
    return "answered"
