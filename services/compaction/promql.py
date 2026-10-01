"""PromQL over all signals, evaluated on the query engine (query.py).

Series:
  metrics   by name: http_server_requests_total, {"http.server.requests"} or {__name__="..."}
            (a bare name matches the metric of that name or with its underscores as dots; a
            counter's _total suffix is optional; <histogram>_count / _sum are its count and sum)
  leasyd.logs           one per log record: rate() is records per second
  leasyd.spans          one per span: rate() is spans per second
  leasyd.span.duration  span durations in seconds, for histogram_quantile(0.95, rate(...[5m]))
Labels are OpenTelemetry attributes and resource attributes by name ({"http.route"="/cart"},
by ("k8s.pod.name")); a bare name with underscores also tries dots (http_route). Plus
service_name / "service.name"; for spans span_name, span_kind (SERVER, CLIENT, ...),
status_code (UNSET, OK, ERROR); for logs severity_text, severity_number, severity_range
(ERROR_FATAL, WARN, INFO, TRACE_DEBUG, UNKNOWN).

Supported: selectors with =, !=, =~, !~; range vectors and offset; rate, increase, irate (as rate),
avg/min/max/sum/count/last_over_time, histogram_quantile (span durations); sum, avg, min, max,
count, group, topk, bottomk, quantile, stddev, stdvar with by/without; + - * / % ^, comparisons
(filtering, or with bool), and, or, unless, with on()/ignoring(); abs, ceil, floor, round, sqrt,
exp, ln, log2, log10, sgn, clamp, clamp_min, clamp_max, scalar, vector, time.

How it runs: each selector is one engine query, grouped by time bucket (the largest bucket that
divides both the step and the range) and by series; windows are then summed from the buckets.
`sum by (L) (rate(x[r]))` (and increase / count_over_time / sum_over_time) is pushed down: the
engine groups by L directly. Logs and spans have no series of their own beyond service.name and
the labels an enclosing `by` names. Counter increases are exact (resets handled, see query.py);
windows start and end on bucket edges.
"""
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import query as engine

LOOKBACK_S = 300          # an instant selector sees the latest point within 5 minutes
MAX_POINTS = 11_000       # per series, as Prometheus
MAX_SERIES = 10_000
MAX_SELECTORS = 20


class PromQLError(engine.BadQuery):
    pass


# ------------------------------------------------------------------ lexer

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
_TOKEN = re.compile(r"""
    (?P<ws>\s+|\#[^\n]*)
  | (?P<duration>(?:\d+(?:ms|s|m|h|d|w|y))+)(?![A-Za-z0-9_])
  | (?P<number>0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)
  | (?P<ident>[A-Za-z_:][A-Za-z0-9_:.]*)
  | (?P<op>=~|!~|==|!=|<=|>=|[-+*/%^<>=(){}\[\],])
""", re.X)


def _unquote(s):
    if s[0] == "`":
        return s[1:-1]
    return json.loads('"' + s[1:-1].replace('\\"', '"').replace("\\'", "'").replace('"', '\\"') + '"')


def tokenize(text):
    out, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            raise PromQLError(f"unexpected character {text[i]!r} at position {i + 1}")
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        v = m.group(kind)
        if kind == "duration":
            v = sum(int(n) * _UNITS[u] for n, u in re.findall(r"(\d+)(ms|s|m|h|d|w|y)", v))
        elif kind == "number":
            v = float(int(v, 16)) if v.lower().startswith("0x") else float(v)
        elif kind == "string":
            v = _unquote(v)
        elif kind == "ident" and v.lower() in ("inf", "nan"):
            kind, v = "number", float(v)
        out.append((kind, v, m.start()))
    out.append(("eof", None, len(text)))
    return out


# ------------------------------------------------------------------ parser (AST: tuples)

AGG_OPS = {"sum", "avg", "min", "max", "count", "group", "topk", "bottomk", "quantile", "stddev", "stdvar"}
RANGE_FNS = {"rate", "increase", "irate", "avg_over_time", "min_over_time", "max_over_time",
             "sum_over_time", "count_over_time", "last_over_time"}
MATH_FNS = {"abs": abs, "ceil": math.ceil, "floor": math.floor, "sqrt": math.sqrt, "exp": math.exp,
            "ln": math.log, "log2": math.log2, "log10": math.log10,
            "sgn": lambda x: (x > 0) - (x < 0)}
OTHER_FNS = {"histogram_quantile", "clamp", "clamp_min", "clamp_max", "round", "scalar", "vector", "time"}
PRECEDENCE = {"or": 1, "and": 2, "unless": 2, "==": 3, "!=": 3, "<": 3, ">": 3, "<=": 3, ">=": 3,
              "+": 4, "-": 4, "*": 5, "/": 5, "%": 5, "^": 6}
COMPARISONS = {"==", "!=", "<", ">", "<=", ">="}


class Parser:
    def __init__(self, text):
        if len(text) > 10_000:
            raise PromQLError("the query is too long (10,000 characters at most)")
        self.toks, self.i, self.selectors = tokenize(text), 0, 0

    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def take(self, kind=None, value=None):
        t = self.peek()
        if (kind and t[0] != kind) or (value is not None and t[1] != value):
            want = value if value is not None else kind
            got = "the end" if t[0] == "eof" else repr(t[1])
            raise PromQLError(f"expected {want} but found {got} at position {t[2] + 1}")
        self.i += 1
        return t

    def is_op(self, value):
        t = self.peek()
        return t[0] == "op" and t[1] == value

    def parse(self):
        e = self.expr(0)
        if self.peek()[0] != "eof":
            t = self.peek()
            raise PromQLError(f"unexpected {t[1]!r} at position {t[2] + 1}")
        return e

    def binop(self):
        t = self.peek()
        if t[0] == "op" and t[1] in PRECEDENCE:
            return t[1]
        if t[0] == "ident" and t[1] in ("and", "or", "unless"):
            return t[1]
        return None

    def expr(self, min_prec):
        lhs = self.unary()
        while True:
            op = self.binop()
            if op is None or PRECEDENCE[op] < min_prec:
                return lhs
            self.i += 1
            mod = {"bool": False, "match": None, "labels": []}
            if self.peek() == ("ident", "bool", self.peek()[2]):
                if op not in COMPARISONS:
                    raise PromQLError("bool is only for comparisons")
                self.i += 1
                mod["bool"] = True
            if self.peek()[0] == "ident" and self.peek()[1] in ("on", "ignoring"):
                mod["match"] = self.take()[1]
                mod["labels"] = self.label_list()
                if self.peek()[0] == "ident" and self.peek()[1] in ("group_left", "group_right"):
                    raise PromQLError("group_left / group_right are not supported yet")
            rhs = self.expr(PRECEDENCE[op] + (0 if op == "^" else 1))
            lhs = ("binary", op, lhs, rhs, mod)

    def unary(self):
        if self.is_op("-") or self.is_op("+"):
            sign = self.take()[1]
            e = self.unary()
            return ("neg", e) if sign == "-" else e
        return self.postfix(self.primary())

    def postfix(self, e):
        while True:
            if self.is_op("["):
                self.i += 1
                d = self.take("duration")[1]
                if self.is_op(":") or (self.peek()[0] == "ident" and str(self.peek()[1]).startswith(":")):
                    raise PromQLError("subqueries are not supported yet")
                self.take("op", "]")
                if e[0] != "selector" or e[3]:
                    raise PromQLError("a range [..] goes right after a series selector")
                e = ("selector", e[1], e[2], d, e[4])
            elif self.peek()[0] == "ident" and self.peek()[1] == "offset":
                self.i += 1
                neg = -1 if self.is_op("-") and self.take() else 1
                d = self.take("duration")[1] * neg
                if e[0] != "selector":
                    raise PromQLError("offset goes right after a series selector")
                e = ("selector", e[1], e[2], e[3], d)
            else:
                return e

    def label_list(self):
        self.take("op", "(")
        out = []
        while not self.is_op(")"):
            t = self.take()
            if t[0] not in ("ident", "string"):
                raise PromQLError(f"expected a label name at position {t[2] + 1}")
            out.append(t[1])
            if not self.is_op(")"):
                self.take("op", ",")
        self.take("op", ")")
        return out

    def primary(self):
        t = self.peek()
        if t[0] == "number":
            self.i += 1
            return ("num", t[1])
        if t[0] == "string":
            self.i += 1
            return ("str", t[1])
        if t[0] == "duration":   # e.g. "5m" where a number was meant
            raise PromQLError(f"unexpected duration at position {t[2] + 1}")
        if self.is_op("("):
            self.i += 1
            e = self.expr(0)
            self.take("op", ")")
            return e
        if self.is_op("{"):
            return self.selector(None)
        if t[0] == "ident":
            name = t[1]
            if name in AGG_OPS and (self.peek(1)[1] in ("(", "by", "without")):
                return self.aggregation()
            if self.peek(1)[0] == "op" and self.peek(1)[1] == "(":
                return self.call()
            self.i += 1
            return self.selector(name)
        raise PromQLError("it ends too early (a bracket or argument is missing)" if t[0] == "eof" else f"unexpected {t[1]!r} at position {t[2] + 1}")

    def aggregation(self):
        op = self.take()[1]
        grouping = None
        if self.peek()[1] in ("by", "without"):
            grouping = (self.take()[1], self.label_list())
        self.take("op", "(")
        param = None
        if op in ("topk", "bottomk", "quantile"):
            param = self.expr(0)
            self.take("op", ",")
        e = self.expr(0)
        self.take("op", ")")
        if self.peek()[1] in ("by", "without"):
            if grouping:
                raise PromQLError("by/without given twice")
            grouping = (self.take()[1], self.label_list())
        return ("agg", op, e, param, grouping)

    def call(self):
        t = self.take()
        name = t[1]
        if name not in RANGE_FNS | set(MATH_FNS) | OTHER_FNS:
            raise PromQLError(f"unknown function {name}() at position {t[2] + 1}")
        self.take("op", "(")
        args = []
        while not self.is_op(")"):
            args.append(self.expr(0))
            if not self.is_op(")") and not self.is_op(","):
                t = self.peek()
                raise PromQLError(f"expected , or ) but found {'the end' if t[0] == 'eof' else repr(t[1])} at position {t[2] + 1}")
            if self.is_op(","):
                self.i += 1
        self.take("op", ")")
        return ("call", name, args)

    def selector(self, name):
        self.selectors += 1
        if self.selectors > MAX_SELECTORS:
            raise PromQLError(f"at most {MAX_SELECTORS} series selectors in one query")
        matchers = []
        if name is not None:
            matchers.append(("__name__", "=", name))
        if self.is_op("{"):
            self.i += 1
            while not self.is_op("}"):
                t = self.take()
                if t[0] not in ("ident", "string"):
                    raise PromQLError(f"expected a label name at position {t[2] + 1}")
                if t[0] == "string" and (self.is_op(",") or self.is_op("}")):
                    matchers.append(("__name__", "=", t[1]))      # {"http.server.requests"}
                else:
                    op = self.take("op")[1]
                    if op not in ("=", "!=", "=~", "!~"):
                        raise PromQLError(f"unknown matcher {op!r}")
                    matchers.append((t[1], op, self.take("string")[1]))
                if not self.is_op("}"):
                    self.take("op", ",")
            self.take("op", "}")
        if not any(m[0] == "__name__" for m in matchers):
            raise PromQLError("a selector needs a metric name, e.g. leasyd.spans{...} or {__name__=\"...\"}")
        return ("selector", tuple(matchers), None, None, 0)


def parse(text):
    return Parser(text).parse()


# ------------------------------------------------------------------ sources and labels

SEVERITY_RANGES = {"ERROR_FATAL": (17, 24), "WARN": (13, 16), "INFO": (9, 12), "TRACE_DEBUG": (1, 8), "UNKNOWN": (0, 0)}
SPAN_KINDS = ["UNSPECIFIED", "INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER"]
STATUS_CODES = ["UNSET", "OK", "ERROR"]
_PSEUDO = {"leasyd.logs": ("logs", "count"), "leasyd_logs": ("logs", "count"), "leasyd_logs_total": ("logs", "count"),
           "leasyd.spans": ("traces", "count"), "leasyd_spans": ("traces", "count"), "leasyd_spans_total": ("traces", "count"),
           "leasyd.span.duration": ("traces", "duration"), "leasyd_span_duration": ("traces", "duration"),
           "leasyd_span_duration_seconds": ("traces", "duration")}


def _source(matchers):
    """-> (signal, kind, where conditions for the name). kind: count | duration | value | count_field | sum_field."""
    names = [m for m in matchers if m[0] == "__name__"]
    if len(names) != 1:
        raise PromQLError("give exactly one metric name per selector")
    _, op, name = names[0]
    if op == "=" and name in _PSEUDO:
        return (*_PSEUDO[name], [])
    if op != "=":   # a regex over metric names
        return "metrics", "value", [{"field": "metric_name", "op": {"=~": "regex", "!~": "not_regex", "!=": "not_in"}[op],
                                     "value": [name] if op == "!=" else name}]
    kind = "value"
    for suffix, k in (("_count", "count_field"), ("_sum", "sum_field"), ("_bucket", "bucket")):
        if name.endswith(suffix):
            name, kind = name[: -len(suffix)], k
    if kind == "bucket":
        raise PromQLError("histogram buckets (_bucket) are not supported yet; for span latency use "
                          "histogram_quantile(0.95, rate(leasyd.span.duration[5m]))")
    candidates = {name, name.replace("_", ".")}
    if name.endswith("_total"):
        base = name[: -len("_total")]
        candidates |= {base, base.replace("_", ".")}
    return "metrics", kind, [{"field": "metric_name", "op": "in", "value": sorted(candidates)}]


def _label_field(signal, label):
    """A PromQL label -> (engine field, how its values are written: None | 'kind' | 'status' | 'range')."""
    special = {"service_name": "service", "service.name": "service"}
    if signal == "traces":
        special |= {"span_name": "name", "span.name": "name", "span_kind": ("kind", "kind"), "span.kind": ("kind", "kind"),
                    "status_code": ("status_code", "status"), "otel.status_code": ("status_code", "status")}
    if signal == "logs":
        special |= {"severity_text": "severity_text", "severity_number": "severity_number",
                    "severity_range": ("severity_number", "range")}
    if label in special:
        f = special[label]
        return f if isinstance(f, tuple) else (f, None)
    if not engine._ATTR_KEY.match(label):
        raise PromQLError(f"bad label name {label!r}")
    keys = [label] + ([label.replace("_", ".")] if "_" in label else [])
    return "label." + "|".join(keys), None


def _encode(how, value):
    """A label value as the engine stores it (span kinds and status codes are numbers)."""
    if how == "kind":
        v = value.upper().removeprefix("SPAN_KIND_")
        return str(SPAN_KINDS.index(v)) if v in SPAN_KINDS else value
    if how == "status":
        v = value.upper().removeprefix("STATUS_CODE_")
        return str(STATUS_CODES.index(v)) if v in STATUS_CODES else value
    return value


def _decode(how, value):
    if value is None:
        return None
    if how == "kind":
        i = int(float(value))
        return SPAN_KINDS[i] if 0 <= i < len(SPAN_KINDS) else str(value)
    if how == "status":
        i = int(float(value))
        return STATUS_CODES[i] if 0 <= i < len(STATUS_CODES) else str(value)
    if how == "range":
        n = int(float(value))
        return next((k for k, (lo, hi) in SEVERITY_RANGES.items() if lo <= n <= hi), "UNKNOWN")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _conditions(signal, matchers):
    """Label matchers -> (engine where, services)."""
    where, services = [], None
    for label, op, value in matchers:
        if label == "__name__":
            continue
        field, how = _label_field(signal, label)
        if how == "range":   # severity_range="ERROR_FATAL" -> a severity_number interval
            if op not in ("=", "!="):
                raise PromQLError("severity_range takes = or !=")
            if value not in SEVERITY_RANGES:
                raise PromQLError(f"severity_range is one of {sorted(SEVERITY_RANGES)}")
            lo, hi = SEVERITY_RANGES[value]
            nums = [str(n) for n in range(lo, hi + 1)] + (["0"] if value == "UNKNOWN" else [])
            where.append({"field": "severity_number", "op": "in" if op == "=" else "not_in", "value": nums})
            continue
        if op in ("=", "!=") and how in ("kind", "status"):
            value = _encode(how, value)
        if op == "=":
            if field == "service" and value:
                services = sorted(set(services or [value]) & {value}) if services is not None else [value]
                continue
            where.append({"field": field, "op": "not_exists"} if value == "" else {"field": field, "op": "=", "value": value})
        elif op == "!=":
            where.append({"field": field, "op": "exists"} if value == "" else {"field": field, "op": "not_in", "value": [value]})
        else:
            where.append({"field": field, "op": "regex" if op == "=~" else "not_regex", "value": value})
    return where, services


# ------------------------------------------------------------------ values

class Vector(dict):
    """{label key: (labels dict, [value or None per timestamp])}"""


def _get(labels, name):
    """A label's value by the name used in the query: exact, else with underscores as dots."""
    if name in labels:
        return labels[name]
    if name == "service.name":
        return labels.get("service_name")
    alt = name.replace("_", ".")
    return labels.get(alt) if alt != name else None


def _pick(labels, names):
    """{name: value} for the names present (as written in the query)."""
    return {n: v for n in names if (v := _get(labels, n)) is not None}


def _without(labels, names):
    drop = set(names) | {n.replace("_", ".") for n in names} | ({"service_name"} if "service.name" in names else set())
    return {k: v for k, v in labels.items() if k not in drop}


def _key(labels):
    return tuple(sorted(labels.items()))


def _drop_name(labels):
    return {k: v for k, v in labels.items() if k != "__name__"}


# ------------------------------------------------------------------ evaluation

class Evaluator:
    def __init__(self, tenant, start, end, step, run_query=None, workers=None):
        if step <= 0:
            raise PromQLError("step must be positive")
        if (end - start) / step + 1 > MAX_POINTS:
            raise PromQLError(f"too many points per series (at most {MAX_POINTS}); use a larger step")
        self.tenant, self.step = tenant, step
        self.times = [start + i * step for i in range(int((end - start) // step) + 1)]
        self.run_query = run_query or (lambda q: engine.run(q))
        self.stats = {"engine_queries": 0, "bytes": 0}
        self.workers = workers

    # --------------------------------------------------------- engine access

    def _bucket(self, window, offset=0):
        """The largest engine time bucket dividing the step, the window, the offset and the evaluation times."""
        for b in sorted(engine.BUCKET_SECONDS, reverse=True):
            if b <= self.step and self.step % b == 0 and window % b == 0 and offset % b == 0 and self.times[0] % b == 0:
                return b
        raise PromQLError("the step and ranges must be multiples of 10 seconds")

    def _query(self, q):
        q = {"tenant": self.tenant, "limit": engine.MAX_INTERNAL_ROWS, "max_rows": engine.MAX_INTERNAL_ROWS, **q}
        out = self.run_query(q)
        self.stats["engine_queries"] += 1
        self.stats["bytes"] += (out.get("stats") or {}).get("bytes", 0)
        if out.get("truncated") or len(out["rows"]) >= engine.MAX_INTERNAL_ROWS:
            raise PromQLError("too many series or points for this query; aggregate it (e.g. sum by (service_name) (...)), "
                              "narrow the selector, or use a larger step")
        return out

    def fetch(self, sel, mode, window, grouping=None):
        """One selector -> Vector. mode: counter (increase per window), avg|min|max|sum|count|last
        (over the window), instant (latest point within LOOKBACK_S), hist (span duration histograms).
        grouping: label names to group by in the engine (pushdown), else the full series."""
        _, matchers, rng, _, offset = sel
        signal, kind, name_where = _source(matchers)
        where, services = _conditions(signal, matchers)
        where = name_where + where
        if mode == "instant":
            window = LOOKBACK_S
        b = self._bucket(window, offset)
        # The engine aggregate per bucket, and how buckets combine over a window.
        field = {"value": "value", "count_field": "count", "sum_field": "sum"}.get(kind)
        if kind == "duration" and mode != "hist":
            raise PromQLError("leasyd.span.duration is for histogram_quantile(φ, rate(leasyd.span.duration[5m]))")
        if mode == "hist" and kind != "duration":
            raise PromQLError("histogram_quantile works on leasyd.span.duration (metric histograms: not yet)")
        if signal != "metrics" and mode not in ("counter", "count", "hist"):
            raise PromQLError(f"{'leasyd.logs' if signal == 'logs' else 'leasyd.spans'} counts records: use rate(), "
                              "increase() or count_over_time() with a range, e.g. rate(leasyd.spans[5m])")
        if mode == "counter":
            aggs = [{"fn": "count"}] if signal != "metrics" else [{"fn": "increase", "field": field}]
        elif mode == "hist":
            aggs = [{"fn": "hist", "field": "duration_ns"}]
        elif mode == "avg":
            aggs = [{"fn": "sum", "field": field}, {"fn": "count"}]
        elif mode == "count":
            aggs = [{"fn": "count"}]
        elif mode in ("instant", "last"):
            aggs = [{"fn": "last", "field": field}]
        else:
            aggs = [{"fn": mode, "field": field}]
        # Series: pushed-down labels, else each metric series (hash), else service for logs/spans.
        if grouping is not None:
            labels = list(dict.fromkeys(grouping))
            fields = [_label_field(signal, l) for l in labels]
        elif signal == "metrics":
            labels, fields = None, [("hash:series", None)]
        else:
            labels, fields = ["service_name"], [("service", None)]
        t0, t1 = self.times[0] - offset - window, self.times[-1] - offset + b
        base = {"signal": signal, "where": where, **({"services": services} if services else {}),
                "start": _iso(t0), "end": _iso(t1)}
        q_main = {**base, "group_by": [f"ts:{b}"] + [f for f, _ in fields], "aggs": aggs}
        jobs = [q_main]
        if labels is None:   # the labels of each metric series
            jobs.append({**base, "group_by": ["hash:series", "metric_name", "service", "json:resource_attributes",
                                              "json:attributes"], "aggs": [{"fn": "count"}]})
        with ThreadPoolExecutor(len(jobs)) as pool:
            results = list(pool.map(self._query, jobs))
        rows = results[0]["rows"]
        if labels is None:
            names = {}
            for h, metric, service, res, attrs, _ in results[1]["rows"]:
                lab = {**json.loads(res or "{}"), **json.loads(attrs or "{}")}
                lab.pop("service.name", None)
                lab["service_name"] = service
                if mode == "instant":
                    lab["__name__"] = metric
                names[h] = {k: v for k, v in lab.items() if v not in (None, "")}
            if len(names) > MAX_SERIES:
                raise PromQLError(f"more than {MAX_SERIES} series; aggregate with sum by (...)")
        # Buckets per series.
        per = {}
        ng = len(fields)
        for r in rows:
            t = _epoch(r[0])
            gvals = r[1:1 + ng]
            vals = r[1 + ng:]
            if labels is None:
                lab = names.get(gvals[0])
                if lab is None:
                    continue
            else:
                lab = {l: _decode(how, v) for l, (_, how), v in zip(labels, fields, gvals)}
                lab = {k: v for k, v in lab.items() if v not in (None, "")}
            k = _key(lab)
            per.setdefault(k, (lab, {}))[1][t] = vals
        # Windows: buckets starting in [t - offset - window, t - offset - b].
        out = Vector()
        span = window
        for k, (lab, buckets) in per.items():
            series = []
            for t in self.times:
                end = t - offset
                ws = [buckets[s] for s in range(end - span, end, b) if s in buckets]
                series.append(_combine(mode, ws, window))
            if any(v is not None for v in series):
                out[k] = (lab, series)
        return out

    # --------------------------------------------------------- expressions

    def eval(self, node, grouping=None):
        kind = node[0]
        if kind == "num":
            return [node[1]] * len(self.times)
        if kind == "str":
            raise PromQLError("a string can't be a query result")
        if kind == "neg":
            v = self.eval(node[1])
            return _map(v, lambda x: -x, drop_name=True)
        if kind == "selector":
            if node[3]:
                raise PromQLError("a range vector (x[5m]) needs a function around it, e.g. rate(x[5m])")
            return self.fetch(node, "instant", 0, grouping if grouping else None)
        if kind == "call":
            return self.call(node[1], node[2], grouping)
        if kind == "agg":
            return self.aggregate(node)
        if kind == "binary":
            return self.binary(node)
        raise PromQLError(f"can't evaluate {kind}")

    def call(self, name, args, grouping=None):
        if name in RANGE_FNS:
            if len(args) != 1 or args[0][0] != "selector" or not args[0][3]:
                raise PromQLError(f"{name}() takes a range vector, e.g. {name}(leasyd.spans[5m])")
            sel, window = args[0], args[0][3]
            mode = {"rate": "counter", "irate": "counter", "increase": "counter", "avg_over_time": "avg",
                    "min_over_time": "min", "max_over_time": "max", "sum_over_time": "sum",
                    "count_over_time": "count", "last_over_time": "last"}[name]
            v = self.fetch(sel, mode, window, grouping)
            if name in ("rate", "irate"):
                v = _map(v, lambda x: x / window, drop_name=True)
            return _map(v, lambda x: x, drop_name=True)
        if name in MATH_FNS:
            if len(args) != 1:
                raise PromQLError(f"{name}() takes one argument")
            f = MATH_FNS[name]
            return _map(self.eval(args[0]), lambda x: _safe(f, x), drop_name=True)
        if name == "time":
            return [float(t) for t in self.times]
        if name == "vector":
            s = self._scalar_arg(args, 0, name)
            return Vector({(): ({}, list(s))})
        if name == "scalar":
            v = self.eval(args[0])
            if not isinstance(v, Vector):
                return v
            if len(v) != 1:
                return [math.nan] * len(self.times)
            return [x if x is not None else math.nan for x in next(iter(v.values()))[1]]
        if name in ("clamp_min", "clamp_max", "clamp", "round"):
            v = self.eval(args[0])
            ps = [self._scalar_arg(args, i, name) for i in range(1, len(args))]
            def g(x, i):
                if name == "clamp_min":
                    return max(x, ps[0][i])
                if name == "clamp_max":
                    return min(x, ps[0][i])
                if name == "clamp":
                    return None if ps[0][i] > ps[1][i] else min(max(x, ps[0][i]), ps[1][i])
                to = ps[0][i] if ps else 1
                return math.floor(x / to + 0.5) * to
            return _map_i(v, g, drop_name=True)
        if name == "histogram_quantile":
            if len(args) != 2:
                raise PromQLError("histogram_quantile(φ, rate(leasyd.span.duration[5m]))")
            phi = self._scalar_arg(args, 0, name)
            inner, grouping = args[1], None
            if inner[0] == "agg":
                if inner[1] != "sum" or inner[3] is not None or (inner[4] and inner[4][0] != "by"):
                    raise PromQLError("inside histogram_quantile use sum by (...) (rate(leasyd.span.duration[5m]))")
                grouping = [l for l in (inner[4][1] if inner[4] else []) if l != "le"]
                inner = inner[2]
            if inner[0] != "call" or inner[1] not in ("rate", "increase") or inner[2][0][0] != "selector" or not inner[2][0][3]:
                raise PromQLError("histogram_quantile(φ, rate(leasyd.span.duration[5m]))")
            sel = inner[2][0]
            v = self.fetch(sel, "hist", sel[3], grouping)
            out = Vector()
            for k, (lab, hists) in v.items():
                vals = []
                for i, h in enumerate(hists):
                    p = phi[i]
                    if h is None or p is None:
                        vals.append(None)
                    elif not 0 <= p <= 1:
                        vals.append(math.inf if p > 1 else -math.inf)
                    else:
                        q = engine._percentile(h, p)
                        vals.append(None if q is None else q / 1e9)   # ns -> seconds
                out[k] = (lab, vals)
            return out
        raise PromQLError(f"unknown function {name}()")

    def _scalar_arg(self, args, i, name):
        if i >= len(args):
            raise PromQLError(f"{name}() needs more arguments")
        v = self.eval(args[i])
        if isinstance(v, Vector):
            raise PromQLError(f"argument {i + 1} of {name}() must be a number")
        return v

    def aggregate(self, node):
        _, op, expr, param, grouping = node
        mode, labels = grouping if grouping else ("by", [])
        # Pushdown: sum by (L) over rate/increase/count_over_time/sum_over_time of a selector
        # (and any aggregation by (L) over logs or spans, whose only series are the labels asked for).
        push = None
        if mode == "by" and expr[0] == "call" and expr[1] in ("rate", "irate", "increase", "count_over_time", "sum_over_time") \
                and expr[2] and expr[2][0][0] == "selector":
            signal = _source(expr[2][0][1])[0]
            if op == "sum" or signal != "metrics":
                push = labels
        v = self.eval(expr, push)
        if not isinstance(v, Vector):
            raise PromQLError(f"{op}() needs a vector, not a number")
        p = self._scalar_arg([param], 0, op) if param is not None else None
        groups = {}
        for lab, vals in v.values():
            lab = _drop_name(lab)
            g = _pick(lab, labels) if mode == "by" else _without(lab, labels)
            groups.setdefault(_key(g), (g, []))[1].append((lab, vals))
        out = Vector()
        n = len(self.times)
        for gk, (g, members) in groups.items():
            if op in ("topk", "bottomk"):
                for i in range(n):
                    present = [(m[1][i], j) for j, m in enumerate(members) if m[1][i] is not None and not math.isnan(m[1][i])]
                    kk = int(p[i]) if p and p[i] is not None else 0
                    chosen = {j for _, j in sorted(present, reverse=(op == "topk"))[:max(kk, 0)]}
                    for j, (lab, vals) in enumerate(members):
                        key = _key(lab)
                        if key not in out:
                            out[key] = (lab, [None] * n)
                        if j in chosen:
                            out[key][1][i] = vals[i]
                continue
            series = []
            for i in range(n):
                xs = [m[1][i] for m in members if m[1][i] is not None]
                series.append(_agg(op, xs, p[i] if p else None) if xs else None)
            if any(x is not None for x in series):
                out[gk] = (g, series)
        if op in ("topk", "bottomk"):
            out = Vector({k: v for k, v in out.items() if any(x is not None for x in v[1])})
        return out

    def binary(self, node):
        _, op, lhs_n, rhs_n, mod = node
        lhs, rhs = self.eval(lhs_n), self.eval(rhs_n)
        n = len(self.times)
        if op in ("and", "or", "unless"):
            if not isinstance(lhs, Vector) or not isinstance(rhs, Vector):
                raise PromQLError(f"{op} works on two vectors")
            return _set_op(op, lhs, rhs, mod, n)
        if not isinstance(lhs, Vector) and not isinstance(rhs, Vector):
            return [_arith(op, a, b, True) for a, b in zip(lhs, rhs)]
        if not isinstance(rhs, Vector):
            return _map_i(lhs, lambda x, i: _cmp_keep(op, x, rhs[i], mod["bool"], x), drop_name=op not in COMPARISONS or mod["bool"])
        if not isinstance(lhs, Vector):
            return _map_i(rhs, lambda x, i: _cmp_keep(op, lhs[i], x, mod["bool"], x), drop_name=op not in COMPARISONS or mod["bool"])
        # vector op vector: one-to-one on all labels, or on() / ignoring()
        def sig(lab):
            lab = _drop_name(lab)
            if mod["match"] == "on":
                return _key(_pick(lab, mod["labels"]))
            if mod["match"] == "ignoring":
                return _key(_without(lab, mod["labels"]))
            return _key(lab)
        right = {}
        for lab, vals in rhs.values():
            s = sig(lab)
            if s in right:
                raise PromQLError("many-to-one matching: several series on the right have the same labels; "
                                  "use on(...) or ignoring(...) to pick labels that match one to one")
            right[s] = vals
        out = Vector()
        seen = set()
        for lab, vals in lhs.values():
            s = sig(lab)
            if s not in right:
                continue
            if s in seen:
                raise PromQLError("many-to-one matching on the left side; use on(...) or ignoring(...)")
            seen.add(s)
            rv = right[s]
            keep_name = op in COMPARISONS and not mod["bool"]
            res = [_cmp_keep(op, a, b, mod["bool"], a) if a is not None and b is not None else None for a, b in zip(vals, rv)]
            out_lab = lab if keep_name else _drop_name(lab)
            if mod["match"] == "on" and not keep_name:
                out_lab = _pick(out_lab, mod["labels"])
            if any(x is not None for x in res):
                out[_key(out_lab)] = (out_lab, res)
        return out


# ------------------------------------------------------------------ helpers

def _combine(mode, ws, window):
    """Bucket aggregates over one window -> the window's value."""
    if not ws:
        return None
    if mode in ("counter", "count", "sum"):
        xs = [w[0] for w in ws if w[0] is not None]
        return float(sum(xs)) if xs else None
    if mode == "avg":
        s = sum(w[0] or 0 for w in ws)
        c = sum(w[1] or 0 for w in ws)
        return s / c if c else None
    if mode in ("min", "max"):
        xs = [w[0] for w in ws if w[0] is not None]
        return float((min if mode == "min" else max)(xs)) if xs else None
    if mode in ("instant", "last"):
        xs = [w[0] for w in ws if w[0] is not None]
        return float(xs[-1]) if xs else None
    if mode == "hist":
        h = {}
        for w in ws:
            for b, c in (w[0] or {}).items():
                h[int(b)] = h.get(int(b), 0) + c
        return h or None
    raise PromQLError(f"unknown mode {mode}")


def _agg(op, xs, p):
    if op == "sum":
        return float(sum(xs))
    if op == "avg":
        return sum(xs) / len(xs)
    if op == "min":
        return float(min(xs))
    if op == "max":
        return float(max(xs))
    if op == "count":
        return float(len(xs))
    if op == "group":
        return 1.0
    if op in ("stddev", "stdvar"):
        m = sum(xs) / len(xs)
        var = sum((x - m) ** 2 for x in xs) / len(xs)
        return math.sqrt(var) if op == "stddev" else var
    if op == "quantile":
        if p is None or math.isnan(p):
            return math.nan
        if p < 0 or p > 1:
            return -math.inf if p < 0 else math.inf
        s = sorted(xs)
        rank = p * (len(s) - 1)
        lo = int(math.floor(rank))
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (rank - lo)
    raise PromQLError(f"unknown aggregation {op}")


def _arith(op, a, b, scalar_bool=False):
    if a is None or b is None:
        return None
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b:
                return a / b
            return math.nan if a == 0 or math.isnan(a) else math.copysign(math.inf, a) * math.copysign(1, b)
        if op == "%":
            return math.fmod(a, b) if b else math.nan
        if op == "^":
            return float(a) ** b
    except (OverflowError, ValueError):
        return math.nan
    if op in COMPARISONS:
        r = {"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        return float(r) if scalar_bool else (a if r else None)
    raise PromQLError(f"unknown operator {op}")


def _cmp_keep(op, a, b, as_bool, keep):
    """Arithmetic, or a comparison: filter (keep the sample) or, with bool, 1/0."""
    if a is None or b is None:
        return None
    if op in COMPARISONS:
        r = {"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        return float(r) if as_bool else (keep if r else None)
    return _arith(op, a, b)


def _set_op(op, lhs, rhs, mod, n):
    def sig(lab):
        lab = _drop_name(lab)
        if mod["match"] == "on":
            return _key(_pick(lab, mod["labels"]))
        if mod["match"] == "ignoring":
            return _key(_without(lab, mod["labels"]))
        return _key(lab)
    present = {}
    for lab, vals in rhs.values():
        s = sig(lab)
        cur = present.setdefault(s, [False] * n)
        for i, x in enumerate(vals):
            cur[i] = cur[i] or x is not None
    out = Vector()
    if op in ("and", "unless"):
        for k, (lab, vals) in lhs.items():
            p = present.get(sig(lab), [False] * n)
            res = [x if (p[i] if op == "and" else not p[i]) else None for i, x in enumerate(vals)]
            if any(x is not None for x in res):
                out[k] = (lab, res)
        return out
    left_present = {}
    for k, (lab, vals) in lhs.items():
        out[k] = (lab, list(vals))
        cur = left_present.setdefault(sig(lab), [False] * n)
        for i, x in enumerate(vals):
            cur[i] = cur[i] or x is not None
    for k, (lab, vals) in rhs.items():
        p = left_present.get(sig(lab), [False] * n)
        res = [None if p[i] else x for i, x in enumerate(vals)]
        if any(x is not None for x in res):
            if k in out:
                out[k] = (lab, [a if a is not None else b for a, b in zip(out[k][1], res)])
            else:
                out[k] = (lab, res)
    return out


def _safe(f, x):
    try:
        return float(f(x))
    except (ValueError, OverflowError):
        return math.nan


def _map(v, f, drop_name=False):
    return _map_i(v, lambda x, i: f(x), drop_name)


def _map_i(v, f, drop_name=False):
    if not isinstance(v, Vector):
        return [f(x, i) if x is not None else None for i, x in enumerate(v)]
    out = Vector()
    for k, (lab, vals) in v.items():
        lab2 = _drop_name(lab) if drop_name else lab
        res = [f(x, i) if x is not None else None for i, x in enumerate(vals)]
        if any(x is not None for x in res):
            key = _key(lab2)
            if key in out and drop_name:
                raise PromQLError("the result has several series with the same labels once the metric name is dropped; "
                                  "aggregate first, e.g. sum by (...)")
            out[key] = (lab2, res)
    return out


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(ts):
    """Engine bucket timestamps ('2026-09-26T10:00:00.000000Z') -> epoch seconds."""
    return int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def _fmt(x):
    if x is None:
        return None
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "+Inf" if x > 0 else "-Inf"
    return repr(float(x)) if not float(x).is_integer() else str(int(x)) if abs(x) < 1e15 else repr(float(x))


# ------------------------------------------------------------------ entry points

def query_range(tenant, text, start, end, step, run_query=None):
    """-> Prometheus API result (matrix or scalar as matrix). start/end: epoch seconds; step: seconds."""
    step = int(step)
    if step < 10 or step % 10:
        raise PromQLError("step must be a multiple of 10 seconds")
    start = int(start) // step * step
    end = int(end) // step * step
    if end < start:
        raise PromQLError("end is before start")
    ast = parse(text)
    ev = Evaluator(tenant, start, end, step, run_query)
    v = ev.eval(ast)
    if not isinstance(v, Vector):
        v = Vector({(): ({}, v)})
    result = [{"metric": lab, "values": [[t, _fmt(x)] for t, x in zip(ev.times, vals) if x is not None]}
              for lab, vals in sorted(v.values(), key=lambda s: _key(s[0]))]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}, "stats": ev.stats}


def query_instant(tenant, text, at, run_query=None):
    at = int(at) // 10 * 10
    ast = parse(text)
    ev = Evaluator(tenant, at, at, 60, run_query)
    v = ev.eval(ast)
    if not isinstance(v, Vector):
        return {"status": "success", "data": {"resultType": "scalar", "result": [at, _fmt(v[0])]}, "stats": ev.stats}
    result = [{"metric": lab, "value": [at, _fmt(vals[0])]} for lab, vals in sorted(v.values(), key=lambda s: _key(s[0]))
              if vals[0] is not None]
    return {"status": "success", "data": {"resultType": "vector", "result": result}, "stats": ev.stats}
