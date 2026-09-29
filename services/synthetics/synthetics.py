"""Synthetic HTTP checks: customers' own uptime and API checks, run from us-east-1.

A check (set up by a signed-in user in the portal) requests a URL on a schedule and asserts on the
answer: status, response time, text in the body. Each run becomes the tenant's own telemetry, put
on its Firehose streams exactly as ingest would (so it shows up in every screen, is isolated,
metered and deleted like any data): a span (service "synthetics"), metrics synthetics.check.success
(1/0), synthetics.check.duration (ms) and synthetics.check.tls_days_remaining, and an ERROR log
when it fails.

Three Lambdas share this module:
  api   /v1/app/checks...  (Cognito: the tenant comes from the user's token, never the request)
        GET list | POST create | POST test (run an unsaved check) | GET/PUT/DELETE one |
        POST {id}/run (run now, recorded)
  tick  every minute: runs the checks that are due (frequency 1, 5 or 15 minutes, spread over
        the period by check id) in batches on the runner
  run   probes a batch of checks in parallel and records the results

Safety: a check may only reach the public internet. Hostnames are resolved once and the request
goes to that address (so DNS can't be switched between the check and the request), and any
private, loopback, link-local or otherwise non-public address is refused, redirects included.
The runner's role can only put records on tenant streams: it can read nothing.

Checks are items of the obs-tenants registry: pk "check#<tenant>#<id>", attribute tenant.
"""

import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import boto3
from boto3.dynamodb.conditions import Attr, Key

import ingest

TABLE = os.environ.get("TENANTS_TABLE", "obs-tenants")
RUN_FUNCTION = os.environ.get("RUN_FUNCTION", "obs-synthetics-run")
LOCATION = os.environ.get("AWS_REGION", "us-east-1")
MAX_CHECKS = int(os.environ.get("MAX_CHECKS_PER_TENANT", "20"))
FREQUENCIES = (1, 5, 15)                     # minutes
METHODS = ("GET", "HEAD", "POST", "PUT", "OPTIONS")
MAX_TIMEOUT_MS, MAX_BODY_READ, MAX_REQUEST_BODY, MAX_REDIRECTS = 20_000, 1 << 20, 16 << 10, 5
BATCH = 25                                   # checks per runner invocation (run in parallel)
USER_AGENT = "Leasyd-Synthetics/1.0 (+https://leasyd.com)"
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
_ID = re.compile(r"^[a-z0-9]{12}$")
_HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_STATUS = re.compile(r"^([1-5]xx|[1-5][0-9][0-9])(,([1-5]xx|[1-5][0-9][0-9]))*$")
_BLOCKED_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}

_table = None


def table():
    global _table
    _table = _table or boto3.resource("dynamodb").Table(TABLE)
    return _table


class Refused(ValueError):
    """A request we won't carry out (bad input, or an address that isn't public)."""


# ------------------------------------------------------------------ checks: validation

def validate(body):
    """A check's settings from user input, normalized; raises Refused with a readable reason."""
    if not isinstance(body, dict):
        raise Refused("expected a JSON object")
    name = str(body.get("name") or "").strip()
    if not 1 <= len(name) <= 80:
        raise Refused("name: 1-80 characters")
    url = str(body.get("url") or "").strip()
    target(url)                                                    # scheme, host, port
    method = str(body.get("method") or "GET").upper()
    if method not in METHODS:
        raise Refused(f"method: one of {', '.join(METHODS)}")
    frequency = int(body.get("frequency") or 5)
    if frequency not in FREQUENCIES:
        raise Refused("frequency: 1, 5 or 15 minutes")
    timeout_ms = int(body.get("timeout_ms") or 10_000)
    if not 1000 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise Refused(f"timeout_ms: 1000-{MAX_TIMEOUT_MS}")
    headers = body.get("headers") or {}
    if not isinstance(headers, dict) or len(headers) > 10:
        raise Refused("headers: at most 10")
    for k, v in headers.items():
        if not _HEADER.match(k) or k.lower() in _BLOCKED_HEADERS or len(str(v)) > 1024 or "\n" in str(v) or "\r" in str(v):
            raise Refused(f"header {k!r} is not allowed")
    req_body = body.get("body")
    if req_body is not None and (method not in ("POST", "PUT") or len(str(req_body)) > MAX_REQUEST_BODY):
        raise Refused(f"body: only for POST or PUT, at most {MAX_REQUEST_BODY // 1024} KB")
    expect = body.get("expect") or {}
    status = str(expect.get("status") or "2xx").replace(" ", "")
    if not _STATUS.match(status):
        raise Refused('expect.status: e.g. "2xx", "200" or "200,204,3xx"')
    max_ms = expect.get("max_ms")
    if max_ms is not None and not 1 <= int(max_ms) <= MAX_TIMEOUT_MS:
        raise Refused(f"expect.max_ms: 1-{MAX_TIMEOUT_MS}")
    contains = expect.get("contains")
    if contains is not None and not 1 <= len(str(contains)) <= 200:
        raise Refused("expect.contains: 1-200 characters")
    return {"name": name, "url": url, "method": method, "frequency": frequency, "timeout_ms": timeout_ms,
            "headers": {str(k): str(v) for k, v in headers.items()},
            **({"body": str(req_body)} if req_body is not None else {}),
            "expect": {"status": status, **({"max_ms": int(max_ms)} if max_ms is not None else {}),
                       **({"contains": str(contains)} if contains is not None else {})},
            "follow_redirects": bool(body.get("follow_redirects", True)),
            "enabled": bool(body.get("enabled", True))}


def target(url):
    """(scheme, host, port, path) of a URL a check may request; raises Refused."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise Refused("url: not a valid URL")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Refused("url: must start with http:// or https://")
    if u.username or u.password:
        raise Refused("url: credentials in the URL are not allowed; use a header")
    if len(url) > 2048:
        raise Refused("url: at most 2048 characters")
    host = u.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".internal", ".local")):
        raise Refused("url: must be a public address")
    try:   # an IP literal must be public too (names are checked after DNS)
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not public(literal):
        raise Refused("url: must be a public address")
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return u.scheme, host, port or (443 if u.scheme == "https" else 80), path


def public(ip):
    """Only globally routable unicast addresses (not private, loopback, link-local such as the
    169.254.169.254 metadata address, carrier-grade NAT, multicast or reserved)."""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host, port):
    """The addresses to use for host, refusing it if any of them isn't public (a name that also
    points inside must not be usable to reach inside)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Refused(f"could not resolve {host}")
    ips = list(dict.fromkeys(i[4][0] for i in infos))
    for ip in ips:
        if not public(ipaddress.ip_address(ip.split("%")[0])):
            raise Refused(f"{host} resolves to a non-public address")
    return ips


# ------------------------------------------------------------------ probing

def probe(check):
    """Run one check: -> result dict (never raises)."""
    started = time.time()
    deadline = time.perf_counter() + check["timeout_ms"] / 1000
    url, method, t = check["url"], check["method"], {}
    body = check.get("body")
    try:
        for hop in range(MAX_REDIRECTS + 1):
            scheme, host, port, path = target(url)
            status, headers, content, t, cert_days = _request(scheme, host, port, path, method, check, body, deadline, t)
            location = headers.get("location")
            if check.get("follow_redirects", True) and status in (301, 302, 303, 307, 308) and location:
                if hop == MAX_REDIRECTS:
                    raise Refused(f"more than {MAX_REDIRECTS} redirects")
                url = urljoin(url, location)
                if status == 303 or (status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                continue
            break
        failure = _assert(check["expect"], status, t["total_ms"], content)
        return {"ok": failure is None, "status": status, "failure": failure, "timings": t, "url": url,
                "bytes": len(content), "tls_days": cert_days, "started": started}
    except Refused as e:
        return {"ok": False, "status": None, "failure": str(e), "timings": t, "url": url, "started": started}
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        reason = "timed out" if isinstance(e, (socket.timeout, TimeoutError)) else f"{type(e).__name__}: {e}"
        return {"ok": False, "status": None, "failure": reason[:300], "timings": t, "url": url, "started": started}


def _left(deadline):
    left = deadline - time.perf_counter()
    if left <= 0:
        raise TimeoutError("timed out")
    return left


def _request(scheme, host, port, path, method, check, body, deadline, t):
    t0 = time.perf_counter()
    ips = resolve(host, port)
    t = {**t, "dns_ms": round((time.perf_counter() - t0) * 1000, 1)}
    sock = socket.create_connection((ips[0], port), timeout=_left(deadline))  # the address we checked
    try:
        t["connect_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        cert_days = None
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            t["tls_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            not_after = sock.getpeercert().get("notAfter")
            if not_after:
                cert_days = round((ssl.cert_time_to_seconds(not_after) - time.time()) / 86400, 1)
        conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, port)
        conn.sock = sock                                       # already connected (and verified)
        sock.settimeout(_left(deadline))
        default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in {"Host": host if default_port else f"{host}:{port}", "User-Agent": USER_AGENT,
                     "Accept": "*/*", **check.get("headers", {})}.items():
            conn.putheader(k, v)
        data = body.encode() if body is not None else None
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = conn.getresponse()
        t["ttfb_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        content = b"" if method == "HEAD" else resp.read(MAX_BODY_READ)
        t["total_ms"] = round((time.perf_counter() - t0) * 1000 + t.get("total_ms", 0), 1)
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, content, t, cert_days
    finally:
        sock.close()


def _assert(expect, status, total_ms, content):
    """None if the answer meets the check's expectations, else why not."""
    if not any((s.endswith("xx") and status // 100 == int(s[0])) or (not s.endswith("xx") and status == int(s))
               for s in expect["status"].split(",")):
        return f"status {status}, expected {expect['status']}"
    if expect.get("max_ms") and total_ms > expect["max_ms"]:
        return f"took {total_ms:.0f} ms, expected under {expect['max_ms']} ms"
    if expect.get("contains") and expect["contains"] not in content.decode("utf-8", "replace"):
        return f"response does not contain {expect['contains']!r}"
    return None


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
    """-> {signal: OTLP JSON doc} for one run."""
    start = int(r["started"] * 1e9)
    end = start + int(r["timings"].get("total_ms", 0) * 1e6) + 1
    trace, span = secrets.token_hex(16), secrets.token_hex(8)
    resource = {"attributes": _attrs({"service.name": "synthetics", "cloud.region": LOCATION,
                                      "synthetics.location": LOCATION})}
    who = {"check.id": check_id, "check.name": check["name"]}
    t = r["timings"]
    span_attrs = {**who, "check.result": "pass" if r["ok"] else "fail", "check.failure": r["failure"],
                  "url.full": r["url"], "http.request.method": check["method"],
                  "http.response.status_code": r["status"], "check.frequency_minutes": check["frequency"],
                  **{f"check.{k}": float(v) for k, v in t.items()}, "check.tls_days_remaining": r.get("tls_days")}
    docs = {"traces": {"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": {"name": "leasyd.synthetics"}, "spans": [{
        "traceId": trace, "spanId": span, "name": check["name"], "kind": 3,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(end), "attributes": _attrs(span_attrs),
        "status": {} if r["ok"] else {"code": 2, "message": r["failure"]}}]}]}]}}
    now = str(end)
    gauges = [("synthetics.check.success", "1", "1 if the check passed, else 0", 1.0 if r["ok"] else 0.0)]
    if "total_ms" in t:
        gauges.append(("synthetics.check.duration", "ms", "Time to a complete answer", float(t["total_ms"])))
    if r.get("tls_days") is not None:
        gauges.append(("synthetics.check.tls_days_remaining", "d", "Days until the TLS certificate expires", float(r["tls_days"])))
    docs["metrics"] = {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": {"name": "leasyd.synthetics"}, "metrics": [
        {"name": n, "unit": u, "description": d, "gauge": {"dataPoints": [{"timeUnixNano": now, "asDouble": v, "attributes": _attrs(who)}]}}
        for n, u, d, v in gauges]}]}]}
    if not r["ok"]:
        docs["logs"] = {"resourceLogs": [{"resource": resource, "scopeLogs": [{"scope": {"name": "leasyd.synthetics"}, "logRecords": [{
            "timeUnixNano": now, "severityNumber": 17, "severityText": "ERROR", "traceId": trace, "spanId": span,
            "body": {"stringValue": f"Check {check['name']!r} failed: {r['failure']}"},
            "attributes": _attrs({**who, "url.full": r["url"]})}]}]}]}
    return docs


def record(tenant, check_id, check, result):
    """Put one run's telemetry on the tenant's streams, as ingest would."""
    for signal, doc in telemetry(check_id, check, result).items():
        records = list(ingest.to_records(signal, doc))
        if ingest.RECORD_COMPRESSION == "gzip":
            import gzip
            records = [gzip.compress(x, compresslevel=6) for x in records]
        ingest.put_records(f"{ingest.STREAM_PREFIX}{tenant}-{signal}", records)


# ------------------------------------------------------------------ storage

def _pk(tenant, check_id):
    return f"check#{tenant}#{check_id}"


def _public_view(item):
    return {k: v for k, v in item.items() if k not in ("pk", "tenant")} | {"id": item["pk"].rsplit("#", 1)[1]}


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
        body = json.loads(event.get("body") or "{}") if method in ("POST", "PUT") else {}
    except ValueError:
        return _http(400, {"error": "body must be JSON"})
    try:
        if resource == "/v1/app/checks" and method == "GET":
            return _http(200, {"checks": [_public_view(i) for i in list_checks(tenant)], "limit": MAX_CHECKS})
        if resource == "/v1/app/checks" and method == "POST":
            check = validate(body)
            if len(list_checks(tenant)) >= MAX_CHECKS:
                raise Refused(f"at most {MAX_CHECKS} checks")
            new_id, now = secrets.token_hex(6), _now()
            item = {"pk": _pk(tenant, new_id), "tenant": tenant, **check, "created_at": now, "updated_at": now, "created_by": user}
            table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
            return _http(201, _public_view(item))
        if resource == "/v1/app/checks/test" and method == "POST":
            check = validate(body)
            return _http(200, {"result": _plain(probe(check))})
        item = get_check(tenant, check_id)
        if item is None:
            return _http(404, {"error": "no such check"})
        if resource == "/v1/app/checks/{id}" and method == "GET":
            return _http(200, _public_view(item))
        if resource == "/v1/app/checks/{id}" and method == "PUT":
            item = {**item, **validate({**_public_view(item), **body}), "updated_at": _now()}
            table().put_item(Item=item)
            return _http(200, _public_view(item))
        if resource == "/v1/app/checks/{id}" and method == "DELETE":
            table().delete_item(Key={"pk": item["pk"]})
            return _http(200, {"deleted": check_id})
        if resource == "/v1/app/checks/{id}/run" and method == "POST":
            result = probe(item)
            record(tenant, check_id, item, result)
            return _http(200, {"result": _plain(result)})
    except Refused as e:
        return _http(400, {"error": str(e)})
    return _http(404, {"error": "unknown route"})


def due(check_id, frequency, minute):
    """Whether a check runs this minute: every `frequency` minutes, offset by its id so checks of
    the same frequency are spread over the period."""
    return (minute + int(check_id, 16)) % int(frequency) == 0


def tick(event, context):
    """Scheduled every minute: hand the due checks to the runner in batches."""
    minute = int(time.time() // 60)
    items, kw = [], dict(FilterExpression=Attr("pk").begins_with("check#") & Attr("enabled").eq(True))
    while True:
        page = table().scan(**kw)
        items += [i for i in page["Items"] if due(i["pk"].rsplit("#", 1)[1], i["frequency"], minute)]
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    lam = boto3.client("lambda")
    for i in range(0, len(items), BATCH):
        lam.invoke(FunctionName=RUN_FUNCTION, InvocationType="Event",
                   Payload=json.dumps({"checks": items[i:i + BATCH]}, default=_json).encode())
    print(json.dumps({"minute": minute, "due": len(items)}))
    return {"due": len(items)}


def run(event, context):
    """Probe a batch of checks in parallel and record each result for its tenant."""
    checks = event.get("checks") or []

    def one(item):
        tenant, check_id = item["tenant"], item["pk"].rsplit("#", 1)[1]
        result = probe(item)
        record(tenant, check_id, item, result)
        return {"tenant": tenant, "check": check_id, "ok": result["ok"], "ms": result["timings"].get("total_ms")}
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
    return str(v)


def _plain(v):
    return json.loads(json.dumps(v, default=_json))


def _http(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, default=_json)}
