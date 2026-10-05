"""Drop rules: a tenant's rules for data to discard as it arrives (debug logs, health-check spans,
noisy metrics). Dropped records are billed for ingest only, never stored.

Rules live in obs-tenants item drop#<tenant> (the account API validates and writes them):

    {"id": "r1", "name": "Debug logs", "signal": "logs", "enabled": true, "keep_percent": 0,
     "conditions": [{"field": "severity_number", "op": "<", "value": 9}]}

A record matches a rule when every condition holds; the first enabled matching rule of its signal
decides: it keeps keep_percent of the matching records (0 drops them all). Sampling is by trace id
when the record has one, so a trace's spans and logs are kept or dropped together.

Fields (the query engine's names, so a rule can be previewed with a query):
  every signal  service, attributes.<key>, resource.<key>
  logs          severity_number, severity_text, body
  traces        name, kind (1 internal .. 5 consumer), status_code (0 unset, 1 ok, 2 error), duration_ns
  metrics       metric_name (and attributes.<key> of each data point)
Ops: = != < <= > >= in contains. Values are compared as text, except < <= > >= (numbers).
"""
import hashlib
import json
import random

OPS = {"=", "!=", "<", "<=", ">", ">=", "in", "contains"}
_METRIC_KINDS = ("gauge", "sum", "histogram", "exponentialHistogram", "summary")


def _any_value(v):
    if not isinstance(v, dict):
        return None
    for k in ("stringValue", "intValue", "doubleValue"):
        if k in v:
            return v[k]
    if "boolValue" in v:
        return "true" if v["boolValue"] else "false"
    if v:
        return json.dumps(next(iter(v.values())), separators=(",", ":"))
    return None


def _attrs(lst):
    return {a.get("key"): _any_value(a.get("value")) for a in lst or [] if isinstance(a, dict)}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _holds(cond, value):
    op, want = cond["op"], cond["value"]
    if op in ("<", "<=", ">", ">="):
        a, b = _num(value), _num(want)
        if a is None or b is None:
            return False
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
    if value is None:
        return op == "!="
    text = str(value)
    if op == "=":
        return text == str(want)
    if op == "!=":
        return text != str(want)
    if op == "in":
        return text in {str(w) for w in (want if isinstance(want, list) else [want])}
    return str(want).lower() in text.lower()   # contains


class _Record:
    """The fields a rule may test, read lazily from one OTLP JSON item and its resource."""

    def __init__(self, fields, attrs, resource):
        self.fields, self.attrs, self.resource = fields, attrs, resource

    def get(self, field):
        if field.startswith("attributes."):
            return self.attrs().get(field[len("attributes."):])
        if field.startswith("resource."):
            return self.resource.get(field[len("resource."):])
        if field == "service":
            return self.resource.get("service.name")
        return self.fields(field)


def _keep(rule, trace_id):
    pct = rule.get("keep_percent") or 0
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    if trace_id:
        h = int(hashlib.blake2b(trace_id.encode(), digest_size=4).hexdigest(), 16) % 10_000
        return h < pct * 100
    return random.random() * 100 < pct


def _decide(rules, rec, trace_id=None):
    """True to keep the record."""
    for rule in rules:
        if all(_holds(c, rec.get(c["field"])) for c in rule["conditions"]):
            return _keep(rule, trace_id)
    return True


def usable(rules, signal):
    """The enabled, well-formed rules of a signal (anything else is ignored, never fails ingest)."""
    out = []
    for r in rules or []:
        try:
            if r.get("signal") != signal or not r.get("enabled", True):
                continue
            conds = r.get("conditions") or []
            if not conds or not all(isinstance(c, dict) and isinstance(c.get("field"), str) and c.get("op") in OPS
                                    and "value" in c for c in conds):
                continue
            out.append(r)
        except AttributeError:
            continue
    return out


def apply(signal, doc, rules):
    """Drop what the rules say from an OTLP JSON document, in place. -> (items received, items dropped);
    items are log records, spans or metric data points."""
    rules = usable(rules, signal)
    top, scope_key, item_key = {"logs": ("resourceLogs", "scopeLogs", "logRecords"),
                                "traces": ("resourceSpans", "scopeSpans", "spans"),
                                "metrics": ("resourceMetrics", "scopeMetrics", "metrics")}[signal]
    received = dropped = 0
    for res in doc.get(top) or []:
        resource = _attrs((res.get("resource") or {}).get("attributes"))
        for scope in res.get(scope_key) or []:
            items = scope.get(item_key) or []
            if signal == "metrics":
                n, d = _metrics(items, rules, resource)
                received += n
                dropped += d
                scope[item_key] = [m for m in items if _points(m)]
                continue
            received += len(items)
            if not rules:
                continue
            kept = [it for it in items if _decide(rules, _item_record(signal, it, resource), it.get("traceId"))]
            dropped += len(items) - len(kept)
            scope[item_key] = kept
    if dropped:   # nothing left of a scope or resource: nothing to store
        for res in doc.get(top) or []:
            res[scope_key] = [sc for sc in res.get(scope_key) or [] if sc.get(item_key)]
        doc[top] = [res for res in doc.get(top) or [] if res.get(scope_key)]
    return received, dropped


def _item_record(signal, it, resource):
    if signal == "logs":
        def fields(f):
            if f == "severity_number":
                return it.get("severityNumber")
            if f == "severity_text":
                return it.get("severityText")
            if f == "body":
                return _any_value(it.get("body"))
            return None
    else:
        def fields(f):
            if f == "name":
                return it.get("name")
            if f == "kind":
                return it.get("kind")
            if f == "status_code":
                return (it.get("status") or {}).get("code", 0)
            if f == "duration_ns":
                try:
                    return int(it.get("endTimeUnixNano")) - int(it.get("startTimeUnixNano"))
                except (TypeError, ValueError):
                    return None
            return None
    cache = {}

    def attrs():
        if "a" not in cache:
            cache["a"] = _attrs(it.get("attributes"))
        return cache["a"]
    return _Record(fields, attrs, resource)


def _points(metric):
    for k in _METRIC_KINDS:
        if isinstance(metric.get(k), dict):
            return metric[k].get("dataPoints") or []
    return []


def _metrics(metrics, rules, resource):
    received = dropped = 0
    for m in metrics:
        pts = _points(m)
        received += len(pts)
        if not rules or not pts:
            continue
        kind = next(k for k in _METRIC_KINDS if isinstance(m.get(k), dict))
        name = m.get("name")
        kept = [p for p in pts
                if _decide(rules, _Record(lambda f: name if f == "metric_name" else None,
                                          lambda p=p: _attrs(p.get("attributes")), resource))]
        dropped += len(pts) - len(kept)
        m[kind]["dataPoints"] = kept
    return received, dropped
