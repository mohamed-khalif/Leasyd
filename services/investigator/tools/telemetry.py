"""The investigation's read-only tools over the tenant's telemetry. Each call becomes an Evidence
record (id ev1, ev2..., what was asked, the window, a short summary, a link to the same data in the
Leasyd app) and the model gets the answer back as data, redacted and cut to size, tagged with that id
so the finding can cite it.

  list_services    every service: requests/s, error %, p95 (a first look)
  query_metrics    PromQL over metrics, spans and logs as series
  query_logs       log records (search) or log groups (top: most frequent messages, by service...)
  query_traces     spans (search), groups of spans (top), or one whole trace (trace)

Times default to the investigation's window (around when the alert started) and must stay within
MAX_SPAN of it, so the agent looks at the incident, not at a month of data.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from agent.redact import redact
from domain.model import Evidence

RESULT_CHARS = 12_000
MAX_SPAN = timedelta(hours=24)

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

    def __init__(self, telemetry, start, end, app_url, clock=time.monotonic):
        self.telemetry, self.window, self.app_url, self.clock = telemetry, (start, end), app_url.rstrip("/"), clock
        self.evidence, self.calls, self.redactions = {}, [], {}

    def _range(self, a):
        start = _parse(a["start"]) if a.get("start") else self.window[0]
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
            start, end = self._range(args)
            sre_name, sre_args, title, link = getattr(self, "_" + name)(args, start, end)
            out = self.telemetry.run(sre_name, {**sre_args, "start": _iso(start), "end": _iso(end)} if sre_name != "get_trace" else sre_args)
        except Exception as e:  # noqa: BLE001  shown to the model, logged on the call
            self.calls.append({"name": name, "input": args, "ms": round((self.clock() - t0) * 1000), "error": str(e)[:300]})
            return f"Error: {str(e)[:500]}", None
        ev = Evidence(id=f"ev{len(self.evidence) + 1}", kind=name.replace("query_", "") if name != "list_services" else "services",
                      title=title, summary=_summary(out), link=link, query={"tool": name, **args}, start=_iso(start), end=_iso(end))
        self.evidence[ev.id] = ev
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


def _cut(v, n):
    s = str(v or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _summary(out):
    if not isinstance(out, dict):
        return ""
    for k, what in (("services", "services"), ("series", "series"), ("records", "log records"), ("spans", "spans"),
                    ("rows", "rows"), ("groups", "groups"), ("tree", "spans in the trace")):
        if isinstance(out.get(k), list):
            return f"{len(out[k])} {what}"
    return "answered"
