"""Synthetic HTTP checks: customers' own multi-step uptime and API monitors, run from us-east-1.

A check (set up by a signed-in user in the portal) is 1-10 HTTP requests ("steps") run in order on
a schedule. Each step can use variables, take new ones from its response, and assert on it:

  variables   {name} anywhere in a step's URL, headers, body, auth or constraint values is replaced
              by: the check's plain variables; its secrets (write-only, encrypted with KMS, never
              shown or recorded); or values extracted by earlier steps.
  auth        none | basic (username, password) | bearer (token), usually from secrets
  extract     from the JSON body (a path such as data.items[0].id), a regex (its first group) or
              a response header, into a variable for the following steps
  constraints status (e.g. "<400", "2xx", "3xx, 404, 406-410, >=500"), response time, body
              contains / not contains / regex, header, JSON value, TLS certificate days left.
              A step without constraints must return a status below 400.
  options     follow redirects, verify TLS certificates, record the response body when it fails
              (first 2 KB, secrets and extracted values masked); cookies carry over between steps.

The first failing step ends the run. Each run becomes the tenant's own telemetry, put on its Firehose
streams exactly as ingest would (so it is isolated, metered, deleted and shown like any data): a
trace (the check, with one span per step; service "synthetics"), metrics synthetics.check.success
(1/0), synthetics.check.duration (ms), synthetics.step.duration and
synthetics.check.tls_days_remaining, and an ERROR log when it fails.

Three Lambdas share this module:
  api   /v1/app/checks...  (Cognito: the tenant comes from the user's token, never the request)
        GET list | POST create | POST test (run unsaved settings) | GET/PUT/DELETE one |
        POST {id}/run (run now, recorded)
  tick  every minute: hands the checks that are due (every 1, 5 or 15 minutes, spread over the
        period by id) to the runner in batches
  run   runs a batch in parallel and records the results

Safety
- Only public addresses: each step's final URL (after variables) is resolved once and requested at
  that address; private, loopback, link-local (169.254.169.254), carrier-grade NAT and other
  non-public addresses are refused, redirects included.
- Secrets: encrypted with the checks KMS key under the context {tenant, check}, so a ciphertext only
  decrypts for its own check. Values are never returned by the API or written to telemetry.
- Customer regexes run with a time limit (a bad pattern can't stall the runner). No customer code runs.
- The runner's role can decrypt check secrets and put records on tenant streams: it reads no data.

Checks are items of the obs-tenants registry: pk "check#<tenant>#<id>", attribute tenant.
"""

import base64
import http.client
import json
import os
import re
import secrets as _secrets
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin

import boto3
import regex
from boto3.dynamodb.conditions import Attr, Key

import ingest
import reliability as rel
import safety  # noqa: F401  (tests patch safety.public through it)
from safety import PLACEHOLDER as _PLACEHOLDER, Refused, mask as _mask, resolve, sub as _sub, target

TABLE = os.environ.get("TENANTS_TABLE", "obs-tenants")
RUN_FUNCTION = os.environ.get("RUN_FUNCTION", "obs-synthetics-run")
KMS_KEY = os.environ.get("KMS_KEY", "alias/obs-checks")
LOCATION = os.environ.get("AWS_REGION", "us-east-1")
MAX_CHECKS = int(os.environ.get("MAX_CHECKS_PER_TENANT", "20"))
FREQUENCIES = (1, 5, 15, 30, 45, 60)            # minutes
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MAX_STEPS, MAX_TOTAL_MS, MAX_STEP_MS = 10, 25_000, 20_000   # a whole check fits in an API call ("Test")
MAX_BODY_READ, MAX_REQUEST_BODY, MAX_REDIRECTS, BODY_SAMPLE = 1 << 20, 16 << 10, 5, 2048
REGEX_TIMEOUT_S, REGEX_TEXT = 0.2, 256 << 10
BATCH = 25                                      # checks per runner invocation (run in parallel)
BROWSER_BATCH = 5                               # browser checks per runner invocation (each waits on a browser)
BROWSER_FUNCTION = os.environ.get("BROWSER_FUNCTION", "obs-synthetics-browser")
ALERTS_FUNCTION = os.environ.get("ALERTS_FUNCTION", "")          # obs-alerts: told of each recorded run
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
BROWSER_MAX_MS, BROWSER_TEST_MS = 60_000, 18_000   # a scheduled browser run; one from the portal (fits an API call)
BROWSER_ACTIONS = {   # action: (required fields, optional fields)
    "navigate": (("url",), ()), "click": (("selector",), ()), "hover": (("selector",), ()),
    "type": (("selector", "text"), ()), "select": (("selector", "value"), ()), "press": (("key",), ("selector",)),
    "wait_for": (("selector",), ()), "wait": (("ms",), ()), "assert_text": (("text",), ("selector",)),
    "assert_no_text": (("text",), ()), "assert_element": (("selector",), ()), "assert_url": (("value",), ()),
    "extract": (("selector", "variable"), ("attribute",)),
}
DEVICES, SCREENSHOTS = ("desktop", "mobile"), ("failure", "every_step")
_KEY = re.compile(r"^(?:(?:Control|Shift|Alt|Meta)\+){0,3}[A-Za-z0-9]{1,12}$")
_RUN = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_USER_AGENT = "Leasyd-Synthetics/1.0 (+https://leasyd.com)"
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
_ID = re.compile(r"^[a-z0-9]{12}$")
_HEADER = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
_BLOCKED_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "cookie"}
_STATUS_TERM = re.compile(r"^(?:([1-5])xx|([1-5]\d\d)\s*-\s*([1-5]\d\d)|(<=|>=|!=|<|>|=)?\s*([1-5]\d\d))$")
JSON_OPS = ("exists", "equals", "not_equals", "contains", "lt", "gt")
HEADER_OPS = ("exists", "equals", "contains")

_table = _kms = _lambda = _s3 = None


def table():
    global _table
    _table = _table or boto3.resource("dynamodb").Table(TABLE)
    return _table


def kms():
    global _kms
    _kms = _kms or boto3.client("kms")
    return _kms


def lam():
    global _lambda
    from botocore.config import Config
    _lambda = _lambda or boto3.client("lambda", config=Config(read_timeout=150, retries={"max_attempts": 0}))
    return _lambda


def s3():
    global _s3
    _s3 = _s3 or boto3.client("s3")
    return _s3


class StepFailed(Exception):
    pass


# ------------------------------------------------------------------ validation

def _text(v, what, lo=0, hi=1024):
    s = "" if v is None else str(v)
    if not lo <= len(s) <= hi:
        raise Refused(f"{what}: {lo}-{hi} characters")
    return s


def _var_name(v, what):
    if not isinstance(v, str) or not _VAR.match(v):
        raise Refused(f"{what}: letters, digits and _, starting with a letter or _ (at most 40)")
    return v


def validate(body):
    """A check's settings from user input, normalized; raises Refused with a readable reason.
    Secret values are not part of the settings (see _secret_values)."""
    if not isinstance(body, dict):
        raise Refused("expected a JSON object")
    name = _text(str(body.get("name") or "").strip(), "name", 1, 80)
    kind = body.get("type") or "http"
    if kind not in ("http", "browser"):
        raise Refused("type: http or browser")
    frequency = int(body.get("frequency") or 5)
    if frequency not in FREQUENCIES:
        raise Refused(f"frequency: {', '.join(map(str, FREQUENCIES[:-1]))} or {FREQUENCIES[-1]} minutes")
    most = BROWSER_MAX_MS if kind == "browser" else MAX_TOTAL_MS
    timeout_ms = int(body.get("timeout_ms") or (30_000 if kind == "browser" else 20_000))
    if not 1000 <= timeout_ms <= most:
        raise Refused(f"timeout_ms (the whole check): 1000-{most}")
    variables = body.get("variables") or {}
    if not isinstance(variables, dict) or len(variables) > 20:
        raise Refused("variables: at most 20")
    variables = {_var_name(k, "variable name"): _text(v, f"variable {k}", 0, 1024) for k, v in variables.items()}
    secret_names = list(body.get("secret_names") or [])
    steps = body.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise Refused(f"steps: 1-{MAX_STEPS}")
    known = set(variables) | set(secret_names)
    out = []
    for i, s in enumerate(steps):
        if kind == "browser":
            step = _validate_browser_step(s, i + 1, known)
            known |= {step["variable"]} if step["action"] == "extract" else set()
        else:
            step = _validate_step(s, i + 1, known, known - set(variables))
            known |= {e["name"] for e in step["extract"]}
        out.append(step)
    check = {"type": kind, "name": name, "frequency": frequency, "timeout_ms": timeout_ms, "variables": variables,
             "steps": out, "enabled": bool(body.get("enabled", True))}
    if kind == "browser":
        if out[0]["action"] != "navigate":
            raise Refused("step 1: a browser check starts by opening a URL (navigate)")
        device, shots = body.get("device") or "desktop", body.get("screenshots") or "failure"
        if device not in DEVICES or shots not in SCREENSHOTS:
            raise Refused(f"device: {' or '.join(DEVICES)}; screenshots: {' or '.join(SCREENSHOTS)}")
        check.update(device=device, screenshots=shots, verify_tls=bool(body.get("verify_tls", True)))
    return check


def _validate_browser_step(s, n, known):
    where = f"step {n}"
    if not isinstance(s, dict):
        raise Refused(f"{where}: expected an object")
    action = s.get("action")
    if action not in BROWSER_ACTIONS:
        raise Refused(f"{where} action: one of {', '.join(BROWSER_ACTIONS)}")
    required, optional = BROWSER_ACTIONS[action]
    step = {"name": _text(str(s.get("name") or f"Step {n}").strip(), f"{where} name", 1, 80), "action": action}
    for f in required + optional:
        v = s.get(f)
        if v in (None, "") and f in optional:
            continue
        if f == "ms":
            v = int(v or 0)
            if not 1 <= v <= 10_000:
                raise Refused(f"{where} wait: 1-10000 ms")
        elif f == "url":
            v = _text(str(v or "").strip(), f"{where} url", 1, 2048)
            if not _PLACEHOLDER.search(v.split("://", 1)[-1].split("/", 1)[0]):
                target(v)                             # a fixed host is checked now; with variables, at run time
        elif f == "key":
            v = str(v or "")
            if not _KEY.match(v):
                raise Refused(f"{where} key: a key such as Enter, Tab, ArrowDown or Control+A")
        elif f == "variable":
            v = _var_name(v, f"{where} variable")
        elif f == "attribute":
            v = str(v)
            if not re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_.:-]{0,63}", v):
                raise Refused(f"{where} attribute: an attribute name such as href or data-id")
        else:
            v = _text(v, f"{where} {f}", 1, 512 if f == "selector" else 1024)
        step[f] = v
    timeout = s.get("timeout_ms")
    if timeout not in (None, ""):
        if not 100 <= int(timeout) <= 30_000:
            raise Refused(f"{where} timeout: 100-30000 ms")
        step["timeout_ms"] = int(timeout)
    unknown = sorted(set(_PLACEHOLDER.findall(json.dumps([step.get(f) for f in ("url", "text", "value")]))) - known)
    if unknown:
        raise Refused(f"{where} uses {', '.join('{' + u + '}' for u in unknown)}: not a variable, secret or earlier extraction")
    return step


def _validate_step(s, n, known, hidden):
    """hidden: the names whose values are never shown or recorded (secrets and earlier extractions)."""
    where = f"step {n}"
    if not isinstance(s, dict):
        raise Refused(f"{where}: expected an object")
    name = _text(str(s.get("name") or f"Step {n}").strip(), f"{where} name", 1, 80)
    url = _text(str(s.get("url") or "").strip(), f"{where} url", 1, 2048)
    if not _PLACEHOLDER.search(url.split("://", 1)[-1].split("/", 1)[0]):
        target(url)                                   # a fixed host is checked now; with variables, at run time
    method = str(s.get("method") or "GET").upper()
    if method not in METHODS:
        raise Refused(f"{where} method: one of {', '.join(METHODS)}")
    headers = s.get("headers") or {}
    if not isinstance(headers, dict) or len(headers) > 20:
        raise Refused(f"{where} headers: at most 20")
    for k, v in headers.items():
        if not _HEADER.match(k) or k.lower() in _BLOCKED_HEADERS:
            raise Refused(f"{where}: header {k!r} is not allowed")
        if len(str(v)) > 4096 or "\n" in str(v) or "\r" in str(v):
            raise Refused(f"{where}: header {k!r} value is not allowed")
    req_body = s.get("body")
    if req_body is not None and len(str(req_body)) > MAX_REQUEST_BODY:
        raise Refused(f"{where} body: at most {MAX_REQUEST_BODY // 1024} KB")
    auth = s.get("auth") or {"type": "none"}
    if auth.get("type") not in ("none", "basic", "bearer"):
        raise Refused(f"{where} auth: none, basic or bearer")
    def secret_ref(v, what):   # passwords and tokens: a secret or an earlier step's extraction, never plain text
        m = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]{0,39})\}", str(v or ""))
        if not m or m[1] not in hidden:
            raise Refused(f"{where} {what}: must be a secret or a value extracted by an earlier step, e.g. {{password}}")
        return m[0]
    auth = ({"type": "basic", "username": _text(auth.get("username"), f"{where} username", 1, 256),
             "password": secret_ref(auth.get("password"), "password")} if auth["type"] == "basic" else
            {"type": "bearer", "token": secret_ref(auth.get("token"), "token")} if auth["type"] == "bearer" else
            {"type": "none"})
    extract = []
    for e in s.get("extract") or []:
        src = e.get("from")
        if src not in ("json", "regex", "header"):
            raise Refused(f"{where} extract: from json, regex or header")
        item = {"name": _var_name(e.get("name"), f"{where} extract name"), "from": src,
                "expr": _text(e.get("expr"), f"{where} extract {e.get('name')}", 1, 256)}
        if src == "json":
            json_path(item["expr"])
        if src == "regex":
            _compile(item["expr"], f"{where} extract {item['name']}")
        extract.append(item)
    if len(extract) > 10:
        raise Refused(f"{where}: at most 10 extractions")
    constraints = [_validate_constraint(c, where) for c in (s.get("constraints") or [])]
    if len(constraints) > 20:
        raise Refused(f"{where}: at most 20 constraints")
    used = set(_PLACEHOLDER.findall(json.dumps([url, headers, req_body, auth, constraints])))
    unknown = sorted(used - known)
    if unknown:
        raise Refused(f"{where} uses {', '.join('{' + u + '}' for u in unknown)}: not a variable, secret or earlier extraction")
    return {"name": name, "method": method, "url": url, "headers": {str(k): str(v) for k, v in headers.items()},
            **({"body": str(req_body)} if req_body not in (None, "") else {}), "auth": auth,
            "user_agent": _text(s.get("user_agent") or DEFAULT_USER_AGENT, f"{where} user agent", 1, 256),
            "follow_redirects": bool(s.get("follow_redirects", True)), "verify_tls": bool(s.get("verify_tls", True)),
            "record_body": bool(s.get("record_body", True)), "extract": extract,
            "constraints": constraints or [{"type": "status", "expr": "<400"}]}


def _validate_constraint(c, where):
    t = c.get("type")
    if t == "status":
        expr = _text(c.get("expr"), f"{where} status", 1, 200)
        status_matches(expr, 200)                     # parses, or raises Refused
        return {"type": t, "expr": expr}
    if t == "max_ms":
        v = int(c.get("value") or 0)
        if not 1 <= v <= MAX_STEP_MS:
            raise Refused(f"{where} response time: 1-{MAX_STEP_MS} ms")
        return {"type": t, "value": v}
    if t in ("body_contains", "body_not_contains"):
        return {"type": t, "value": _text(c.get("value"), f"{where} text", 1, 500)}
    if t == "body_regex":
        return {"type": t, "value": _text(_compile(c.get("value"), f"{where} regex").pattern, "", 1, 256)}
    if t == "header":
        op = c.get("op") or "exists"
        if op not in HEADER_OPS or not _HEADER.match(str(c.get("name") or "")):
            raise Refused(f"{where} header constraint: a header name and one of {', '.join(HEADER_OPS)}")
        return {"type": t, "name": c["name"], "op": op, **({"value": _text(c.get("value"), f"{where} header value", 1, 500)} if op != "exists" else {})}
    if t == "json":
        op = c.get("op") or "exists"
        if op not in JSON_OPS:
            raise Refused(f"{where} JSON constraint: one of {', '.join(JSON_OPS)}")
        path = _text(c.get("path"), f"{where} JSON path", 1, 256)
        json_path(path)
        return {"type": t, "path": path, "op": op, **({"value": _text(c.get("value"), f"{where} JSON value", 0, 500)} if op != "exists" else {})}
    if t == "tls_days":
        v = int(c.get("value") or 0)
        if not 1 <= v <= 365:
            raise Refused(f"{where} certificate: 1-365 days")
        return {"type": t, "value": v}
    raise Refused(f"{where}: unknown constraint {t!r}")


def _compile(pattern, what):
    try:
        return regex.compile(_text(pattern, what, 1, 256))
    except regex.error as e:
        raise Refused(f"{what}: not a valid regular expression ({e})")


def status_matches(expr, status):
    """Whether a status matches an expression like "<400", "2xx", "200,204", "406-410", ">=500"
    (comma-separated terms; any may match). Raises Refused if the expression is malformed."""
    for term in (t.strip() for t in expr.split(",")):
        m = _STATUS_TERM.match(term)
        if not m:
            raise Refused(f'status: {term!r} is not like 2xx, 200, 406-410 or >=500')
        cls, lo, hi, op, n = m.groups()
        if cls and status // 100 == int(cls):
            return True
        if lo and int(lo) <= status <= int(hi):
            return True
        if n and {"<": status < int(n), "<=": status <= int(n), ">": status > int(n), ">=": status >= int(n),
                  "!=": status != int(n), "=": status == int(n), None: status == int(n)}[op]:
            return True
    return False


_PATH_TOKEN = re.compile(r"""\.?([A-Za-z0-9_\-]+)|\[(\d+)\]|\[['"]([^'"\]]+)['"]\]""")


def json_path(path):
    """A simple JSON path -> keys: "data.items[0].id", "$.token", "items[2]['full name']"."""
    p = path.strip()
    p = p[1:] if p.startswith("$") else p
    keys, pos = [], 0
    while pos < len(p):
        m = _PATH_TOKEN.match(p, pos)
        if not m or m.end() == pos:
            raise Refused(f"JSON path {path!r}: use keys and [index], e.g. data.items[0].id")
        keys.append(int(m[2]) if m[2] is not None else (m[1] or m[3]))
        pos = m.end()
    if not keys:
        raise Refused(f"JSON path {path!r} is empty")
    return keys


def json_get(doc, path):
    """(found, value) at path in a parsed JSON document."""
    cur = doc
    for k in json_path(path):
        if isinstance(k, int) and isinstance(cur, list) and -len(cur) <= k < len(cur):
            cur = cur[k]
        elif isinstance(k, str) and isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return False, None
    return True, cur


# ------------------------------------------------------------------ running a check

def run_check(check, secret_values):
    """Run every step in order until one fails -> result dict (never raises).
    secret_values: {name: plaintext} (decrypted for this run only)."""
    started, deadline = time.time(), time.perf_counter() + check["timeout_ms"] / 1000
    values = {**check.get("variables", {}), **secret_values}
    masked = [v for v in secret_values.values() if v]          # never shown or recorded
    cookies, steps, failure = {}, [], None
    for i, step in enumerate(check["steps"]):
        r = _run_step(step, values, cookies, deadline, masked)
        steps.append(r)
        if not r["ok"]:
            failure = f"{step['name']}: {r['failure']}"
            break
    total = sum(s["timings"].get("total_ms", 0) for s in steps)
    tls = [s["tls_days"] for s in steps if s.get("tls_days") is not None]
    return {"ok": failure is None, "failure": failure, "failed_step": None if failure is None else len(steps) - 1,
            "steps": steps, "total_ms": round(total, 1), "tls_days": min(tls) if tls else None, "started": started}


def _run_step(step, values, cookies, deadline, masked):
    started, t, status, content, url = time.time(), {}, None, b"", step["url"]
    try:
        url = _sub(step["url"], values)
        method, body = step["method"], _sub(step["body"], values) if "body" in step else None
        headers = {"User-Agent": _sub(step["user_agent"], values), "Accept": "*/*"}
        headers.update({k: _sub(v, values) for k, v in step["headers"].items()})
        a = step["auth"]
        if a["type"] == "basic":
            pair = f"{_sub(a['username'], values)}:{_sub(a['password'], values)}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        elif a["type"] == "bearer":
            headers["Authorization"] = "Bearer " + _sub(a["token"], values)
        if any("\r" in v or "\n" in v for v in headers.values()):
            raise StepFailed("a header value contains a line break (from a variable?)")
        tls_days = None
        for hop in range(MAX_REDIRECTS + 1):
            scheme, host, port, path = target(url)
            jar = "; ".join(f"{k}={v}" for k, v in cookies.get(host, {}).items())
            status, resp_headers, content, t, cert = _request(scheme, host, port, path, method,
                                                               {**headers, **({"Cookie": jar} if jar else {})},
                                                               body, deadline, t, step["verify_tls"])
            tls_days = cert if cert is not None else tls_days
            for sc in resp_headers.get("set-cookie", []):
                name, _, rest = sc.partition("=")
                if name.strip():
                    cookies.setdefault(host, {})[name.strip()] = rest.split(";", 1)[0]
            location = (resp_headers.get("location") or [None])[0]
            if step["follow_redirects"] and status in (301, 302, 303, 307, 308) and location:
                if hop == MAX_REDIRECTS:
                    raise StepFailed(f"more than {MAX_REDIRECTS} redirects")
                url = urljoin(url, location)
                if status == 303 or (status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                continue
            break
        text = content.decode("utf-8", "replace")
        failure = _check_constraints(step["constraints"], status, t.get("total_ms", 0), text, resp_headers, tls_days, values)
        extracted = {}
        if failure is None:
            for e in step["extract"]:
                v = _extract(e, text, resp_headers)
                if v is None:
                    failure = f"could not extract {{{e['name']}}} ({e['from']} {e['expr']})"
                    break
                extracted[e["name"]] = v
            values.update(extracted)
            masked += [v for v in extracted.values() if len(v) >= 4]   # tokens and ids: don't record them
        return {"name": step["name"], "ok": failure is None, "failure": failure, "status": status, "url": _mask(url, masked),
                "timings": t, "tls_days": tls_days, "extracted": sorted(extracted), "started": started,
                "body_sample": _mask(text[:BODY_SAMPLE], masked) if failure and step["record_body"] else None}
    except (Refused, StepFailed) as e:
        reason = str(e)
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        reason = "timed out" if isinstance(e, (socket.timeout, TimeoutError)) else f"{type(e).__name__}: {e}"
    return {"name": step["name"], "ok": False, "failure": _mask(reason[:300], masked), "status": status,
            "url": _mask(url, masked), "timings": t, "tls_days": None, "extracted": [], "started": started, "body_sample": None}


def _left(deadline):
    left = deadline - time.perf_counter()
    if left <= 0:
        raise TimeoutError("timed out")
    return min(left, MAX_STEP_MS / 1000)


def _request(scheme, host, port, path, method, headers, body, deadline, t, verify):
    """One HTTP exchange at the address we checked. -> status, {header: [values]}, body, timings, cert days."""
    t0 = time.perf_counter()
    ips = resolve(host, port)
    t = {"dns_ms": round((time.perf_counter() - t0) * 1000, 1)}
    sock = socket.create_connection((ips[0], port), timeout=_left(deadline))
    try:
        t["connect_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        cert_days = None
        if scheme == "https":
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
            t["tls_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            not_after = (sock.getpeercert() or {}).get("notAfter")
            if not_after:
                cert_days = round((ssl.cert_time_to_seconds(not_after) - time.time()) / 86400, 1)
        conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, port)
        conn.sock = sock                                       # already connected (and verified)
        sock.settimeout(_left(deadline))
        default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if default_port else f"{host}:{port}")
        for k, v in headers.items():
            conn.putheader(k, v)
        data = body.encode() if body is not None else None
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = conn.getresponse()
        t["ttfb_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        content = b"" if method == "HEAD" else resp.read(MAX_BODY_READ)
        t["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out = {}
        for k, v in resp.getheaders():
            out.setdefault(k.lower(), []).append(v)
        return resp.status, out, content, t, cert_days
    finally:
        sock.close()


def _check_constraints(constraints, status, total_ms, text, headers, tls_days, values):
    """None if the response meets every constraint, else why not."""
    doc, parsed = None, False
    for c in constraints:
        t = c["type"]
        if t == "status" and not status_matches(c["expr"], status):
            return f"status {status}, expected {c['expr']}"
        if t == "max_ms" and total_ms > c["value"]:
            return f"took {total_ms:.0f} ms, expected at most {c['value']} ms"
        if t == "body_contains" and _sub(c["value"], values) not in text:
            return f"response does not contain {c['value']!r}"
        if t == "body_not_contains" and _sub(c["value"], values) in text:
            return f"response contains {c['value']!r}"
        if t == "body_regex" and not _search(c["value"], text):
            return f"response does not match /{c['value']}/"
        if t == "header":
            got = (headers.get(c["name"].lower()) or [None])[0]
            want = _sub(c.get("value", ""), values)
            if got is None or (c["op"] == "equals" and got != want) or (c["op"] == "contains" and want not in got):
                return f"header {c['name']} is {got!r}" + ("" if c["op"] == "exists" else f", expected it to {c['op'].replace('_', ' ')} {c.get('value')!r}")
        if t == "json":
            if not parsed:
                parsed = True
                try:
                    doc = json.loads(text)
                except ValueError:
                    return "response is not JSON"
            found, got = json_get(doc, c["path"])
            if not _json_ok(found, got, c["op"], _sub(c.get("value", ""), values)):
                shown = json.dumps(got)[:80] if found else "missing"
                return f"{c['path']} is {shown}, expected {c['op'].replace('_', ' ')}" + (f" {c.get('value')!r}" if "value" in c else "")
        if t == "tls_days" and tls_days is not None and tls_days < c["value"]:
            return f"TLS certificate expires in {tls_days:.0f} days (less than {c['value']})"
    return None


def _json_ok(found, got, op, want):
    if op == "exists":
        return found
    if not found:
        return False
    as_text = got if isinstance(got, str) else json.dumps(got)
    if op in ("equals", "not_equals"):
        same = as_text == want or (not isinstance(got, str) and _num(want) is not None and _num(as_text) == _num(want))
        return same if op == "equals" else not same
    if op == "contains":
        return want in as_text
    a, b = _num(as_text), _num(want)
    return a is not None and b is not None and (a < b if op == "lt" else a > b)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _search(pattern, text):
    try:
        return regex.search(pattern, text[:REGEX_TEXT], timeout=REGEX_TIMEOUT_S)
    except TimeoutError:
        raise StepFailed(f"regex /{pattern}/ took too long")


def _extract(e, text, headers):
    if e["from"] == "header":
        return (headers.get(e["expr"].lower()) or [None])[0]
    if e["from"] == "regex":
        m = _search(e["expr"], text)
        return None if not m else (m.group(1) if m.re.groups else m.group(0))
    try:
        found, v = json_get(json.loads(text), e["expr"])
    except ValueError:
        return None
    return None if not found or v is None else (v if isinstance(v, str) else json.dumps(v))


def run_any(check, secret_values, budget_ms=None):
    """Run a check of either type -> result (never raises)."""
    if check.get("type") == "browser":
        return run_browser_check(check, secret_values, budget_ms)
    return run_check(check, secret_values)


def _settings(item):
    return {k: v for k, v in item.items() if k not in ("pk", "tenant", "secrets", "created_at", "updated_at", "created_by", "maintenance")}


def run_browser_check(check, secret_values, budget_ms=None):
    """Run a browser check in the browser function (browser.py), which has no AWS permissions of
    its own: it gets this check's settings and decrypted secrets only."""
    started = time.time()
    try:
        resp = lam().invoke(FunctionName=BROWSER_FUNCTION, Payload=json.dumps(
            {"check": _settings(check), "secrets": secret_values, "budget_ms": budget_ms or check["timeout_ms"]}, default=_json).encode())
        out = json.loads(resp["Payload"].read() or b"null")
        if resp.get("FunctionError") or not isinstance(out, dict) or "steps" not in out:
            raise RuntimeError(out.get("errorMessage", "no result") if isinstance(out, dict) else "no result")
        return out
    except Exception as e:  # noqa: BLE001  report it as this check's failure
        return {"ok": False, "failure": _mask(f"the browser could not run ({type(e).__name__}: {str(e)[:200]})", list(secret_values.values())),
                "failed_step": None, "steps": [], "total_ms": 0.0, "tls_days": None, "started": started}


def screenshot_key(tenant, check_id, run_id, step):
    return f"synthetics/tenant={tenant}/{check_id}/{run_id}/{int(step)}.jpg"


def store_screenshots(tenant, check_id, result):
    """Save a run's screenshots under the tenant's own prefix (kept 30 days, see the bucket's
    lifecycle); the step then records that it has one."""
    result.setdefault("run_id", _secrets.token_hex(16))
    for i, s in enumerate(result["steps"]):
        if s.get("screenshot") and DATA_BUCKET:
            s3().put_object(Bucket=DATA_BUCKET, Key=screenshot_key(tenant, check_id, result["run_id"], i + 1),
                            Body=base64.b64decode(s["screenshot"]), ContentType="image/jpeg")
            s["screenshot_saved"] = True
    return result


# ------------------------------------------------------------------ results as telemetry

def _attrs(d):
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            out.append({"key": k, "value": {"doubleValue": v}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


def telemetry(check_id, check, r):
    """-> {signal: OTLP JSON doc} for one run: a trace with a span per step, metrics, a log on failure."""
    start = int(r["started"] * 1e9)
    trace, root = r.get("run_id") or _secrets.token_hex(16), _secrets.token_hex(8)
    resource = {"attributes": _attrs({"service.name": "synthetics", "cloud.region": LOCATION, "synthetics.location": LOCATION})}
    who = {"check.id": check_id, "check.name": check["name"], "check.type": check.get("type", "http"),
           "check.run_id": trace, "check.excluded": r.get("excluded")}   # excluded: run in a maintenance window
    spans, end = [], start
    for i, s in enumerate(r["steps"]):
        s_start = int(s["started"] * 1e9)
        s_end = s_start + int(s["timings"].get("total_ms", 0) * 1e6) + 1
        end = max(end, s_end)
        spans.append({"traceId": trace, "spanId": _secrets.token_hex(8), "parentSpanId": root, "name": s["name"], "kind": 3,
                      "startTimeUnixNano": str(s_start), "endTimeUnixNano": str(s_end),
                      "attributes": _attrs({**who, "step.index": i + 1, "step.name": s["name"], "url.full": s["url"],
                                            "http.response.status_code": s["status"], "step.result": "pass" if s["ok"] else "fail",
                                            "step.failure": s["failure"], "step.extracted": ",".join(s["extracted"]) or None,
                                            **{f"step.{k}": float(v) for k, v in s["timings"].items()},
                                            **_browser_attrs(s)}),
                      "status": {} if s["ok"] else {"code": 2, "message": s["failure"]}})
    first = r["steps"][0] if r["steps"] else {"url": check["steps"][0]["url"], "status": None}
    spans.insert(0, {"traceId": trace, "spanId": root, "name": check["name"], "kind": 1,
                     "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(end, start + 1)),
                     "attributes": _attrs({**who, "check.result": "pass" if r["ok"] else "fail", "check.failure": r["failure"],
                                           "check.steps": len(check["steps"]), "check.steps_run": len(r["steps"]),
                                           "check.failed_step": None if r["failed_step"] is None else r["failed_step"] + 1,
                                           "url.full": first["url"], "http.response.status_code": first["status"],
                                           "check.total_ms": float(r["total_ms"]), "check.frequency_minutes": check["frequency"],
                                           "check.tls_days_remaining": r.get("tls_days"),
                                           "check.screenshots": ",".join(str(i + 1) for i, s in enumerate(r["steps"]) if s.get("screenshot_saved")) or None}),
                     "status": {} if r["ok"] else {"code": 2, "message": r["failure"]}})
    docs = {"traces": {"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": {"name": "leasyd.synthetics"}, "spans": spans}]}]}}
    now = str(max(end, start + 1))
    points = [("synthetics.check.success", "1", "1 if the check passed, else 0", 1.0 if r["ok"] else 0.0, who),
              ("synthetics.check.duration", "ms", "Time for all the check's steps", float(r["total_ms"]), who)]
    points += [("synthetics.step.duration", "ms", "Time for one step", float(s["timings"]["total_ms"]),
                {**who, "step.index": i + 1, "step.name": s["name"]}) for i, s in enumerate(r["steps"]) if "total_ms" in s["timings"]]
    if r.get("tls_days") is not None:
        points.append(("synthetics.check.tls_days_remaining", "d", "Days until the soonest TLS certificate expires", float(r["tls_days"]), who))
    for i, s in enumerate(r["steps"]):
        for key, (metric, unit, desc) in VITALS.items():
            v = (s.get("vitals") or {}).get(key)
            if v is not None:
                points.append((metric, unit, desc, float(v), {**who, "step.index": i + 1, "step.name": s["name"]}))
    by_name = {}
    for n, u, d, v, a in points:
        by_name.setdefault((n, u, d), []).append({"timeUnixNano": now, "asDouble": v, "attributes": _attrs(a)})
    docs["metrics"] = {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": {"name": "leasyd.synthetics"}, "metrics": [
        {"name": n, "unit": u, "description": d, "gauge": {"dataPoints": dps}} for (n, u, d), dps in by_name.items()]}]}]}
    if not r["ok"]:
        failed = r["steps"][r["failed_step"]] if r["failed_step"] is not None else {}
        body = f"Check {check['name']!r} failed at {r['failure']}"
        if failed.get("body_sample"):
            body += f"\n--- response (first {BODY_SAMPLE} bytes) ---\n{failed['body_sample']}"
        for key, title in (("console_errors", "browser console errors"), ("http_errors", "responses with errors"),
                           ("failed_requests", "failed requests"), ("blocked", "blocked (not public)")):
            if failed.get(key):
                body += f"\n--- {title} ---\n" + "\n".join(failed[key])
        docs["logs"] = {"resourceLogs": [{"resource": resource, "scopeLogs": [{"scope": {"name": "leasyd.synthetics"}, "logRecords": [{
            "timeUnixNano": now, "severityNumber": 17, "severityText": "ERROR", "traceId": trace, "spanId": root,
            "body": {"stringValue": body},
            "attributes": _attrs({**who, "step.name": failed.get("name"), "url.full": failed.get("url"),
                                  "http.response.status_code": failed.get("status")})}]}]}]}
    return docs


VITALS = {   # browser step vitals -> metrics
    "ttfb_ms": ("synthetics.browser.ttfb", "ms", "Time to first byte of the page"),
    "fcp_ms": ("synthetics.browser.fcp", "ms", "First contentful paint"),
    "lcp_ms": ("synthetics.browser.lcp", "ms", "Largest contentful paint"),
    "cls": ("synthetics.browser.cls", "1", "Cumulative layout shift"),
    "load_ms": ("synthetics.browser.load", "ms", "Time until the page's load event"),
}


def _browser_attrs(s):
    if "action" not in s:
        return {}
    return {"step.action": s["action"], "step.screenshot": bool(s.get("screenshot_saved")) or None,
            "step.console_errors": len(s.get("console_errors") or []) or None, "step.http_errors": len(s.get("http_errors") or []) or None,
            "step.blocked": ",".join(s.get("blocked") or []) or None,
            **{f"step.{k}": float(v) for k, v in (s.get("vitals") or {}).items() if v is not None}}


def record(tenant, check_id, check, result):
    """Put one run's telemetry on the tenant's streams, as ingest would; then tell the alerts
    function (asynchronously; a run is never held up or failed by alerting)."""
    import gzip
    for signal, doc in telemetry(check_id, check, result).items():
        records = list(ingest.to_records(signal, doc))
        if ingest.RECORD_COMPRESSION == "gzip":
            records = [gzip.compress(x, compresslevel=6) for x in records]
        ingest.put_records(f"{ingest.STREAM_PREFIX}{tenant}-{signal}", records)
    if ALERTS_FUNCTION:
        try:
            lam().invoke(FunctionName=ALERTS_FUNCTION, InvocationType="Event", Payload=json.dumps({
                "action": "on_result", "tenant": tenant, "check_id": check_id, "check_name": check["name"], "ok": result["ok"],
                "failure": result.get("failure"), "excluded": result.get("excluded"), "run_id": result.get("run_id")}).encode())
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"alerts_not_told": check_id, "error": str(e)[:200]}))


# ------------------------------------------------------------------ secrets

def _context(tenant, check_id):
    return {"tenant": tenant, "check": check_id}


def encrypt_secrets(tenant, check_id, plain, stored=None):
    """Update a check's encrypted secrets: plain {name: value} sets, {name: None} removes."""
    out = dict(stored or {})
    for name, value in (plain or {}).items():
        _var_name(name, "secret name")
        if value is None:
            out.pop(name, None)
        else:
            value = _text(value, f"secret {name}", 1, 4096)
            blob = kms().encrypt(KeyId=KMS_KEY, Plaintext=value.encode(), EncryptionContext=_context(tenant, check_id))["CiphertextBlob"]
            out[name] = base64.b64encode(blob).decode()
    if len(out) > 20:
        raise Refused("secrets: at most 20")
    return out


def decrypt_secrets(tenant, check_id, stored):
    """{name: plaintext}: only decrypts under this check's own tenant and id."""
    return {name: kms().decrypt(CiphertextBlob=base64.b64decode(blob), EncryptionContext=_context(tenant, check_id))["Plaintext"].decode()
            for name, blob in (stored or {}).items()}


# ------------------------------------------------------------------ storage

def _pk(tenant, check_id):
    return f"check#{tenant}#{check_id}"


def _public_view(item):
    """What the portal sees: settings and secret names, never secret values."""
    out = {k: v for k, v in item.items() if k not in ("pk", "tenant", "secrets")}
    return {**out, "id": item["pk"].rsplit("#", 1)[1], "secret_names": sorted(item.get("secrets") or {})}


def _items(tenant, prefix):
    """The tenant's obs-tenants items whose pk starts with prefix (by-tenant index)."""
    items, kw = [], dict(IndexName="by-tenant", KeyConditionExpression=Key("tenant").eq(tenant) & Key("pk").begins_with(prefix))
    while True:
        page = table().query(**kw)
        items += [_plain(i) for i in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return items
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def exclusions(tenant, check_id=None):
    """Live excluded runs (of one check, or all the tenant's); expired ones are removed."""
    now = _now()
    out = []
    for it in _items(tenant, f"exclude#{tenant}#" + (f"{check_id}#" if check_id else "")):
        if it.get("expires_at", "") <= now:
            table().delete_item(Key={"pk": it["pk"]})
            continue
        out.append({k: it.get(k) for k in ("check", "run_id", "reason", "by", "at")})
    return sorted(out, key=lambda e: e["at"] or "", reverse=True)


def windows(tenant):
    return [{**w, "id": w["pk"].rsplit("#", 1)[1]} for w in _items(tenant, f"window#{tenant}#")]


def list_checks(tenant):
    items, kw = [], dict(IndexName="by-tenant", KeyConditionExpression=Key("tenant").eq(tenant) & Key("pk").begins_with(f"check#{tenant}#"))
    while True:
        page = table().query(**kw)
        items += [_plain(i) for i in page["Items"]]      # DynamoDB numbers -> int/float
        if "LastEvaluatedKey" not in page:
            return sorted(items, key=lambda i: i.get("name", "").lower())
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_check(tenant, check_id):
    if not _ID.match(check_id or ""):
        return None
    item = table().get_item(Key={"pk": _pk(tenant, check_id)}, ConsistentRead=True).get("Item")
    return _plain(item) if item and item.get("tenant") == tenant else None


# ------------------------------------------------------------------ Lambda handlers

def api(event, context):
    """The portal's /v1/app/checks routes. The tenant is the signed-in user's custom:tenant claim,
    set by API Gateway's Cognito authorizer; a tenant in the request is never used."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    tenant, user = claims.get("custom:tenant"), claims.get("email")
    if not tenant or not _TENANT.match(tenant):
        return _http(401, {"error": "no tenant for this user"})
    method, resource = event.get("httpMethod"), event.get("resource") or ""
    check_id = (event.get("pathParameters") or {}).get("id")
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):    # the API treats every body as binary (BinaryMediaTypes */*)
            raw = base64.b64decode(raw)
        body = json.loads(raw) if method in ("POST", "PUT") else {}
    except ValueError:
        return _http(400, {"error": "body must be JSON"})
    try:
        if resource.startswith(("/v1/app/windows", "/v1/app/slos")):
            return _settings_api(tenant, user, method, resource, check_id, body)
        if resource == "/v1/app/checks" and method == "GET":
            excluded = {}
            for e in exclusions(tenant):
                excluded.setdefault(e["check"], []).append(e["run_id"])
            return _http(200, {"checks": [{**_public_view(i), "excluded_runs": excluded.get(i["pk"].rsplit("#", 1)[1], [])}
                                          for i in list_checks(tenant)], "limit": MAX_CHECKS})
        if resource == "/v1/app/checks" and method == "POST":
            plain = body.get("secrets") or {}
            check = validate({**body, "secret_names": [k for k, v in plain.items() if v is not None]})
            if len(list_checks(tenant)) >= MAX_CHECKS:
                raise Refused(f"at most {MAX_CHECKS} checks")
            new_id, now = _secrets.token_hex(6), _now()
            item = {"pk": _pk(tenant, new_id), "tenant": tenant, **check, "created_at": now, "updated_at": now, "created_by": user,
                    "secrets": encrypt_secrets(tenant, new_id, plain)}
            table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
            return _http(201, _public_view(item))
        if resource == "/v1/app/checks/test" and method == "POST":
            # Unsaved settings; secrets as typed, or, when editing, the saved ones not retyped.
            saved = get_check(tenant, body.get("id")) if body.get("id") else None
            plain = {**(decrypt_secrets(tenant, body["id"], saved.get("secrets")) if saved else {}),
                     **{k: v for k, v in (body.get("secrets") or {}).items() if v is not None}}
            check = validate({**body, "secret_names": list(plain)})
            return _http(200, {"result": _view_result(run_any(check, plain, BROWSER_TEST_MS))})
        item = get_check(tenant, check_id)
        if item is None:
            return _http(404, {"error": "no such check"})
        if resource == "/v1/app/checks/{id}" and method == "GET":
            return _http(200, {**_public_view(item), "exclusions": exclusions(tenant, check_id)})
        if resource == "/v1/app/checks/{id}/exclusions" and method == "POST":
            e = rel.validate_exclusion(body)
            if len(exclusions(tenant, check_id)) >= rel.MAX_EXCLUSIONS:
                raise Refused(f"at most {rel.MAX_EXCLUSIONS} excluded runs per check")
            table().put_item(Item={"pk": f"exclude#{tenant}#{check_id}#{e['run_id']}", "tenant": tenant, "check": check_id,
                                   **e, "by": user, "at": _now(), "expires_at": rel.exclusion_expires(datetime.now(timezone.utc))})
            return _http(201, {"check": check_id, **e, "by": user})
        if resource == "/v1/app/checks/{id}/exclusions/{run}" and method == "DELETE":
            run_id = (event.get("pathParameters") or {}).get("run") or ""
            if not rel.is_run(run_id):
                raise Refused("run: a run's trace id")
            table().delete_item(Key={"pk": f"exclude#{tenant}#{check_id}#{run_id}"})
            return _http(200, {"included": run_id})
        if resource == "/v1/app/checks/{id}" and method == "PUT":
            secrets_ = encrypt_secrets(tenant, check_id, body.get("secrets"), item.get("secrets"))
            settings = validate({**_public_view(item), **body, "secret_names": list(secrets_)})
            item = {**item, **settings, "secrets": secrets_, "updated_at": _now()}
            table().put_item(Item=item)
            return _http(200, _public_view(item))
        if resource == "/v1/app/checks/{id}" and method == "DELETE":
            table().delete_item(Key={"pk": item["pk"]})
            for e in _items(tenant, f"exclude#{tenant}#{check_id}#"):
                table().delete_item(Key={"pk": e["pk"]})
            return _http(200, {"deleted": check_id})
        if resource == "/v1/app/checks/{id}/run" and method == "POST":
            result = run_any(item, decrypt_secrets(tenant, check_id, item.get("secrets")), BROWSER_TEST_MS)
            window = rel.open_window(windows(tenant), check_id, datetime.now(timezone.utc))
            if window:
                result["excluded"] = f"maintenance window: {window}"
            record(tenant, check_id, item, store_screenshots(tenant, check_id, result))
            return _http(200, {"result": _view_result(result)})
        if resource == "/v1/app/checks/{id}/screenshot" and method == "GET":
            q = event.get("queryStringParameters") or {}
            run_id, step = str(q.get("run") or ""), str(q.get("step") or "")
            if not _RUN.match(run_id) or not step.isdigit() or not 1 <= int(step) <= MAX_STEPS:
                raise Refused("run (a run's trace id) and step (1-10)")
            try:
                obj = s3().get_object(Bucket=DATA_BUCKET, Key=screenshot_key(tenant, check_id, run_id, step))
            except s3().exceptions.NoSuchKey:
                return _http(404, {"error": "no screenshot for that run and step (they are kept 30 days)"})
            return _http(200, {"image": base64.b64encode(obj["Body"].read()).decode(), "content_type": "image/jpeg"})
    except Refused as e:
        return _http(400, {"error": str(e)})
    return _http(404, {"error": "unknown route"})


SETTINGS = {"/v1/app/windows": ("window", rel.MAX_WINDOWS, rel.validate_window),
            "/v1/app/slos": ("slo", rel.MAX_SLOS, rel.validate_slo)}


def _settings_api(tenant, user, method, resource, item_id, body):
    """Maintenance windows and SLOs: list and create; get, replace and delete one."""
    base = resource.removesuffix("/{id}")
    kind, most, validate_ = SETTINGS[base]
    known = {i["pk"].rsplit("#", 1)[1] for i in list_checks(tenant)}
    view = lambda it: {**{k: v for k, v in it.items() if k not in ("pk", "tenant")}, "id": it["pk"].rsplit("#", 1)[1]}  # noqa: E731
    if resource == base:
        if method == "GET":
            return _http(200, {"items": sorted((view(i) for i in _items(tenant, f"{kind}#{tenant}#")), key=lambda i: i["name"].lower()),
                               "limit": most})
        if method == "POST":
            settings = validate_(body, known)
            if len(_items(tenant, f"{kind}#{tenant}#")) >= most:
                raise Refused(f"at most {most}")
            now = _now()
            item = {"pk": f"{kind}#{tenant}#{_secrets.token_hex(6)}", "tenant": tenant, **settings,
                    "created_at": now, "updated_at": now, "created_by": user}
            table().put_item(Item=_dynamo(item), ConditionExpression="attribute_not_exists(pk)")
            return _http(201, view(item))
    elif rel.is_id(item_id):
        pk = f"{kind}#{tenant}#{item_id}"
        item = table().get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
        if item and item.get("tenant") == tenant:
            item = _plain(item)
            if method == "GET":
                return _http(200, view(item))
            if method == "PUT":
                item = {**item, **validate_({**view(item), **body}, known), "updated_at": _now()}
                table().put_item(Item=_dynamo(item))
                return _http(200, view(item))
            if method == "DELETE":
                table().delete_item(Key={"pk": pk})
                return _http(200, {"deleted": item_id})
    return _http(404, {"error": f"no such {'maintenance window' if kind == 'window' else 'SLO'}"})


def _dynamo(v):
    """Floats (an SLO's target) as DynamoDB numbers."""
    from decimal import Decimal
    return json.loads(json.dumps(v), parse_float=Decimal)


def _view_result(r):
    return _plain({k: v for k, v in r.items() if k != "started"} | {"steps": [{k: v for k, v in s.items() if k != "started"} for s in r["steps"]]})


def due(check_id, frequency, minute):
    """Whether a check runs this minute: every `frequency` minutes, offset by its id so checks of
    the same frequency are spread over the period."""
    return (minute + int(check_id, 16)) % int(frequency) == 0


def tick(event, context):
    """Scheduled every minute: hand the due checks to the runner in batches."""
    minute = int(time.time() // 60)
    now = datetime.fromtimestamp(minute * 60, timezone.utc)
    items, open_windows = [], {}
    kw = dict(FilterExpression=(Attr("pk").begins_with("check#") & Attr("enabled").eq(True)) | Attr("pk").begins_with("window#"))
    while True:
        page = table().scan(**kw)
        for i in page["Items"]:
            if i["pk"].startswith("window#"):
                open_windows.setdefault(i["tenant"], []).append(_plain(i))
            elif due(i["pk"].rsplit("#", 1)[1], i["frequency"], minute):
                items.append(i)
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    for i in items:     # runs in an open maintenance window run, but are recorded as excluded
        window = rel.open_window(open_windows.get(i["tenant"], []), i["pk"].rsplit("#", 1)[1], now)
        if window:
            i["maintenance"] = window
    client = boto3.client("lambda")
    http_ = [i for i in items if i.get("type", "http") != "browser"]
    browser_ = [i for i in items if i.get("type") == "browser"]
    for group, size in ((http_, BATCH), (browser_, BROWSER_BATCH)):
        for i in range(0, len(group), size):
            client.invoke(FunctionName=RUN_FUNCTION, InvocationType="Event",
                          Payload=json.dumps({"checks": group[i:i + size]}, default=_json).encode())
    print(json.dumps({"minute": minute, "due": len(items), "browser": len(browser_)}))
    return {"due": len(items)}


def run(event, context):
    """Run a batch of checks in parallel and record each result for its tenant."""
    checks = event.get("checks") or []

    def one(item):
        tenant, check_id = item["tenant"], item["pk"].rsplit("#", 1)[1]
        try:
            plain = decrypt_secrets(tenant, check_id, item.get("secrets"))
        except Exception as e:   # noqa: BLE001  a secret that can't be decrypted fails this check only
            result = {"ok": False, "failure": f"could not decrypt the check's secrets ({type(e).__name__})", "failed_step": None,
                      "steps": [], "total_ms": 0.0, "tls_days": None, "started": time.time()}
        else:
            result = run_any(item, plain)
        if item.get("maintenance"):
            result["excluded"] = f"maintenance window: {item['maintenance']}"
        record(tenant, check_id, item, store_screenshots(tenant, check_id, result))
        return {"tenant": tenant, "check": check_id, "ok": result["ok"], "ms": result["total_ms"]}
    with ThreadPoolExecutor(max(1, len(checks))) as pool:
        out = list(pool.map(one, checks))
    print(json.dumps({"ran": len(out), "failed": sum(1 for o in out if not o["ok"])}))
    return {"ran": out}


# ------------------------------------------------------------------ helpers

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(v).decode()
    return str(v)


def _plain(v):
    return json.loads(json.dumps(v, default=_json))


def _http(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, default=_json)}
