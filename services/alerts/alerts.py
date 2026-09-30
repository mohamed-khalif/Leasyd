"""Alerts (A1): tell a tenant's people when a synthetic check keeps failing or an SLO burns its
error budget, by email, Slack or webhook.

Settings (obs-tenants items, attribute tenant; managed in the portal through /v1/app/alerts/...):
  channel#<tenant>#<id>   where alerts go:
                            email    an SNS topic obs-alert-<tenant>-<id> with that address
                                     subscribed (the person confirms once, by the link AWS sends)
                            slack    an incoming-webhook URL (https://hooks.slack.com/...)
                            webhook  any public https URL; POSTed JSON, signed with the channel's
                                     own secret (X-Leasyd-Signature: sha256=<HMAC of the body>)
                          URLs and signing secrets are encrypted with the checks' KMS key under the
                          context {tenant, channel}; the API shows only their ends.
  alert#<tenant>#<id>     a rule:
                            check_failing  a check (or any check) failed `failures` runs in a row;
                                           resolved by its next pass
                            slo_burn       an SLO burned its budget faster than `burn_rate` over the
                                           last hour, or has less than `budget_below` % of it left;
                                           resolved when neither holds
  astate#<tenant>#<rule>#<subject>   a rule's state per check or SLO (consecutive failures, firing)

Runs excluded by hand or in a maintenance window never count. Each firing and resolution is also
written as the tenant's own log (service "alerts"), so the portal shows the history and it expires
with the rest of the data.

Handlers (one Lambda, obs-alerts):
  api        /v1/app/alerts/{proxy+}: rules, channels, channels/{id}/test (Cognito; the tenant is the user's)
  on_result  invoked (async) by the synthetic runner after each recorded run
  evaluate   every 5 minutes: SLO rules, through the query engine (obs-query) as the portal does
"""

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets as _secrets
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key

import ingest
import safety
from safety import Refused

TABLE = os.environ.get("TENANTS_TABLE", "obs-tenants")
KMS_KEY = os.environ.get("KMS_KEY", "alias/obs-checks")
QUERY_FUNCTION = os.environ.get("QUERY_FUNCTION", "obs-query")
APP_URL = os.environ.get("APP_URL", "https://app.leasyd.com")
MAX_RULES, MAX_CHANNELS = 50, 20
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
_ID = re.compile(r"^[a-z0-9]{12}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
SUCCESS, DURATION = "synthetics.check.success", "synthetics.check.duration"

_clients = {}


def client(name):
    if name not in _clients:
        _clients[name] = boto3.resource("dynamodb").Table(TABLE) if name == "table" else boto3.client(name)
    return _clients[name]


# ------------------------------------------------------------------ settings

def _text(v, what, lo, hi):
    s = "" if v is None else str(v).strip()
    if not lo <= len(s) <= hi:
        raise Refused(f"{what}: {lo}-{hi} characters")
    return s


def validate_channel(body):
    """-> (settings, plaintext secrets to encrypt)."""
    if not isinstance(body, dict):
        raise Refused("expected a JSON object")
    kind = body.get("type")
    name = _text(body.get("name"), "name", 1, 80)
    if kind == "email":
        email = _text(body.get("email"), "email", 3, 254).lower()
        if not _EMAIL.match(email):
            raise Refused("email: not an email address")
        return {"name": name, "type": kind, "email": email}, {}
    if kind in ("slack", "webhook"):
        url = _text(body.get("url"), "url", 10, 2048)
        if not url.startswith("https://"):
            raise Refused("url: must start with https://")
        if kind == "slack" and not url.startswith("https://hooks.slack.com/"):
            raise Refused("url: a Slack incoming-webhook URL, https://hooks.slack.com/services/...")
        safety.target(url)                   # a public host (checked again, after DNS, when sending)
        plain = {"url": url}
        if kind == "webhook":
            plain["secret"] = _secrets.token_hex(24)
        return {"name": name, "type": kind, "url_hint": _hint(url)}, plain
    raise Refused("type: email, slack or webhook")


def _hint(url):
    host = url.split("/")[2]
    return f"https://{host}/…{url[-4:]}"


def validate_rule(body, checks, slos, channels):
    if not isinstance(body, dict):
        raise Refused("expected a JSON object")
    kind = body.get("type")
    chans = list(body.get("channels") or [])
    if not chans or any(c not in channels for c in chans):
        raise Refused("channels: choose one or more of your alert channels")
    out = {"name": _text(body.get("name"), "name", 1, 80), "type": kind, "channels": sorted(set(chans)),
           "enabled": bool(body.get("enabled", True))}
    if kind == "check_failing":
        ids = list(body.get("checks") or [])
        if ids != ["*"] and (not ids or any(i not in checks for i in ids)):
            raise Refused("checks: choose checks, or all of them")
        failures = int(body.get("failures") or 2)
        if not 1 <= failures <= 10:
            raise Refused("failures: 1-10 runs in a row")
        return {**out, "checks": ids if ids == ["*"] else sorted(set(ids)), "failures": failures}
    if kind == "slo_burn":
        if body.get("slo") not in slos:
            raise Refused("slo: choose one of your SLOs")
        rate = body.get("burn_rate")
        below = body.get("budget_below")
        rate = None if rate in (None, "") else float(rate)
        below = None if below in (None, "") else float(below)
        if rate is None and below is None:
            raise Refused("set burn_rate, budget_below, or both")
        if rate is not None and not 1 <= rate <= 1000:
            raise Refused("burn_rate: 1-1000 (times the rate that would use exactly the budget)")
        if below is not None and not 0 <= below <= 100:
            raise Refused("budget_below: 0-100 (%)")
        return {**out, "slo": body["slo"], **({"burn_rate": rate} if rate is not None else {}),
                **({"budget_below": below} if below is not None else {})}
    raise Refused("type: check_failing or slo_burn")


# ------------------------------------------------------------------ storage

def _items(tenant, prefix):
    items, kw = [], dict(IndexName="by-tenant", KeyConditionExpression=Key("tenant").eq(tenant) & Key("pk").begins_with(prefix))
    while True:
        page = client("table").query(**kw)
        items += [_plain(i) for i in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return items
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _get(pk, tenant):
    it = client("table").get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
    return _plain(it) if it and it.get("tenant") == tenant else None


def _id_of(item):
    return item["pk"].rsplit("#", 1)[1]


def _context(tenant, channel_id):
    return {"tenant": tenant, "channel": channel_id}


def _encrypt(tenant, channel_id, plain):
    return {k: base64.b64encode(client("kms").encrypt(KeyId=KMS_KEY, Plaintext=v.encode(),
                                                      EncryptionContext=_context(tenant, channel_id))["CiphertextBlob"]).decode()
            for k, v in plain.items()}


def _decrypt(tenant, channel_id, stored):
    return {k: client("kms").decrypt(CiphertextBlob=base64.b64decode(v), EncryptionContext=_context(tenant, channel_id))["Plaintext"].decode()
            for k, v in (stored or {}).items()}


def _topic_name(tenant, channel_id):
    return f"obs-alert-{tenant}-{channel_id}"


def _public_channel(item):
    out = {k: v for k, v in item.items() if k not in ("pk", "tenant", "secrets", "topic_arn", "subscription_arn")}
    out["id"] = _id_of(item)
    if item["type"] == "email":
        out["status"] = _email_status(item)
    return out


def _email_status(item):
    try:
        subs = client("sns").list_subscriptions_by_topic(TopicArn=item["topic_arn"])["Subscriptions"]
    except Exception:  # noqa: BLE001
        return "unknown"
    arn = next((s["SubscriptionArn"] for s in subs if s.get("Endpoint") == item["email"]), None)
    return "confirmed" if arn and arn.startswith("arn:") else "waiting for confirmation"


def create_channel(tenant, user, body):
    settings, plain = validate_channel(body)
    if len(_items(tenant, f"channel#{tenant}#")) >= MAX_CHANNELS:
        raise Refused(f"at most {MAX_CHANNELS} channels")
    cid = _secrets.token_hex(6)
    item = {"pk": f"channel#{tenant}#{cid}", "tenant": tenant, **settings, "created_by": user, "created_at": _now()}
    if settings["type"] == "email":
        sns = client("sns")
        topic = sns.create_topic(Name=_topic_name(tenant, cid), Attributes={"DisplayName": "Leasyd alerts"},
                                 Tags=[{"Key": "project", "Value": "obs"}, {"Key": "tenant", "Value": tenant}])["TopicArn"]
        sub = sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=settings["email"], ReturnSubscriptionArn=True)
        item.update(topic_arn=topic, subscription_arn=sub["SubscriptionArn"])
    else:
        item["secrets"] = _encrypt(tenant, cid, plain)
    client("table").put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    out = _public_channel(item)
    if settings["type"] == "webhook":
        out["signing_secret"] = plain["secret"]           # shown once, to verify our signatures
    return out


def delete_channel(tenant, item):
    if item.get("topic_arn"):
        client("sns").delete_topic(TopicArn=item["topic_arn"])
    client("table").delete_item(Key={"pk": item["pk"]})


# ------------------------------------------------------------------ sending

def notify(tenant, channel, message):
    """Send one alert message to one channel. -> None, or why it failed (never raises)."""
    try:
        if channel["type"] == "email":
            client("sns").publish(TopicArn=channel["topic_arn"], Subject=message["title"][:99], Message=message["text"])
            return None
        s = _decrypt(tenant, _id_of(channel), channel.get("secrets"))
        if channel["type"] == "slack":
            body = json.dumps({"text": f"*{message['title']}*\n{message['text']}"}).encode()
            status = post(s["url"], body, {"Content-Type": "application/json"})
        else:
            body = json.dumps(message["payload"], separators=(",", ":")).encode()
            sig = hmac.new(s["secret"].encode(), body, hashlib.sha256).hexdigest()
            status = post(s["url"], body, {"Content-Type": "application/json", "X-Leasyd-Signature": f"sha256={sig}",
                                           "User-Agent": "Leasyd-Alerts/1.0"})
        return None if 200 <= status < 300 else f"HTTP {status}"
    except (Refused, OSError, http.client.HTTPException, ssl.SSLError) as e:
        return f"{type(e).__name__}: {str(e)[:200]}"
    except Exception as e:  # noqa: BLE001  a channel that can't be used must not stop the others
        return f"{type(e).__name__}"


def post(url, body, headers, timeout=10):
    """POST to a public https URL only, at the address that was checked (like synthetic checks)."""
    scheme, host, port, path = safety.target(url)
    ip = safety.resolve(host, port)[0]
    sock = socket.create_connection((ip, port), timeout=timeout)
    try:
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, port, timeout=timeout)
        conn.sock = sock
        conn.request("POST", path, body=body, headers={**headers, "Host": host})
        return conn.getresponse().status
    finally:
        sock.close()


def message(tenant, rule, state, subject, detail, link):
    word = "FIRING" if state == "firing" else "RESOLVED"
    title = f"[Leasyd] {word}: {rule['name']} — {subject}"
    text = f"{detail}\n\nOpen in Leasyd: {link}\nRule: {rule['name']}"
    return {"title": title, "text": text, "payload": {
        "tenant": tenant, "state": state, "rule": {"id": _id_of(rule), "name": rule["name"], "type": rule["type"]},
        "subject": subject, "detail": detail, "url": link, "time": _now()}}


def fire(tenant, rule, state, subject, detail, link):
    """Notify every channel of the rule and record the event as the tenant's log."""
    msg = message(tenant, rule, state, subject, detail, link)
    results = {}
    for cid in rule["channels"]:
        channel = _get(f"channel#{tenant}#{cid}", tenant)
        results[cid] = notify(tenant, channel, msg) if channel else "channel deleted"
    record(tenant, rule, state, subject, detail, link, results)
    return results


def record(tenant, rule, state, subject, detail, link, results):
    """The event as a log of the tenant (service "alerts"): the portal's alert history."""
    import gzip
    now = str(time.time_ns())
    attrs = {"alert.rule_id": _id_of(rule), "alert.rule": rule["name"], "alert.type": rule["type"], "alert.state": state,
             "alert.subject": subject, "alert.url": link,
             "alert.failed_channels": ",".join(c for c, r in results.items() if r) or None}
    doc = {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "alerts"}}]},
                             "scopeLogs": [{"scope": {"name": "leasyd.alerts"}, "logRecords": [{
                                 "timeUnixNano": now, "severityNumber": 13 if state == "firing" else 9,
                                 "severityText": "WARN" if state == "firing" else "INFO",
                                 "body": {"stringValue": f"{'FIRING' if state == 'firing' else 'RESOLVED'}: {rule['name']} — {subject}. {detail}"},
                                 "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in attrs.items() if v is not None]}]}]}]}
    records = list(ingest.to_records("logs", doc))
    if ingest.RECORD_COMPRESSION == "gzip":
        records = [gzip.compress(x) for x in records]
    ingest.put_records(f"{ingest.STREAM_PREFIX}{tenant}-logs", records)


# ------------------------------------------------------------------ check rules (after each run)

def _state(tenant, rule_id, subject):
    return client("table").get_item(Key={"pk": f"astate#{tenant}#{rule_id}#{subject}"}).get("Item") or {}


def _set_state(tenant, rule_id, subject, **kw):
    client("table").put_item(Item={"pk": f"astate#{tenant}#{rule_id}#{subject}", "tenant": tenant, **kw, "updated_at": _now()})


def on_result(event, context=None):
    """One recorded run: {tenant, check_id, check_name, ok, excluded, failure}."""
    tenant, check_id = event["tenant"], event["check_id"]
    if not _TENANT.match(tenant or "") or not _ID.match(check_id or ""):
        return {"skipped": "bad event"}
    if event.get("excluded"):
        return {"skipped": "excluded run"}                # maintenance windows never alert
    fired = []
    for rule in _items(tenant, f"alert#{tenant}#"):
        if rule["type"] != "check_failing" or not rule.get("enabled", True):
            continue
        if rule["checks"] != ["*"] and check_id not in rule["checks"]:
            continue
        rid = _id_of(rule)
        st = _state(tenant, rid, check_id)
        count, firing = int(st.get("count", 0)), bool(st.get("firing"))
        link = f"{APP_URL}/#/synthetics/{check_id}"
        if event["ok"]:
            if firing:
                fire(tenant, rule, "resolved", event["check_name"], "The check passed again.", link)
                fired.append((rid, "resolved"))
            if count or firing:
                _set_state(tenant, rid, check_id, count=0, firing=False)
            continue
        count += 1
        if count >= int(rule["failures"]) and not firing:
            detail = f"Failed {count} run{'s' if count > 1 else ''} in a row. Last failure: {event.get('failure') or 'unknown'}"
            fire(tenant, rule, "firing", event["check_name"], detail, link)
            fired.append((rid, "firing"))
            firing = True
        _set_state(tenant, rid, check_id, count=count, firing=firing)
    return {"fired": fired}


# ------------------------------------------------------------------ SLO rules (every 5 minutes)

def slo_status(tenant, slo, excluded, now):
    """good/total over the SLO's window and the last hour, from the checks' metrics -> numbers as
    the portal shows them (services/web/src/pages/Slos.tsx evaluate())."""
    def counts(start):
        base = {"tenant": tenant, "signal": "metrics", "start": _iso(start), "end": _iso(now), "services": ["synthetics"]}
        scope = [{"field": "attributes.check.id", "op": "in", "value": slo["checks"]},
                 {"field": "attributes.check.excluded", "op": "not_exists"},
                 *([{"field": "attributes.check.run_id", "op": "not_in", "value": excluded}] if excluded else [])]
        if slo["type"] == "availability":
            r = _query({**base, "where": [{"field": "metric_name", "op": "=", "value": SUCCESS}, *scope],
                        "aggs": [{"fn": "sum", "field": "value"}, {"fn": "count"}]})
            row = (r.get("rows") or [[0, 0]])[0]
            return float(row[0] or 0), int(row[1] or 0)
        runs = [{"field": "metric_name", "op": "=", "value": DURATION}, *scope]
        total = _query({**base, "where": runs, "aggs": [{"fn": "count"}]})
        good = _query({**base, "where": [*runs, {"field": "value", "op": "<=", "value": slo["threshold_ms"]}], "aggs": [{"fn": "count"}]})
        return float(((good.get("rows") or [[0]])[0][0]) or 0), int(((total.get("rows") or [[0]])[0][0]) or 0)
    good, total = counts(now - timedelta(days=int(slo["window_days"])))
    good1h, total1h = counts(now - timedelta(hours=1))
    return evaluate_numbers(float(slo["target"]), good, total, good1h, total1h)


def evaluate_numbers(target, good, total, good1h=0, total1h=0):
    allowed = (100 - target) / 100
    bad = total - good
    budget_left = None if not total else (1 - bad / (total * allowed)) if allowed > 0 else (1.0 if not bad else float("-inf"))
    burn1h = ((total1h - good1h) / total1h) / allowed if total1h and allowed > 0 else None
    return {"good": good, "total": total, "attainment": good / total * 100 if total else None,
            "budget_left": budget_left, "burn1h": burn1h}


def _query(q):
    r = client("lambda").invoke(FunctionName=QUERY_FUNCTION, Payload=json.dumps(q).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    if r.get("FunctionError") or "error" in out and "rows" not in out:
        raise RuntimeError(f"query failed: {out.get('errorMessage') or out.get('error')}")
    return out


def evaluate(event=None, context=None):
    """Every 5 minutes: each enabled slo_burn rule of each tenant."""
    now = datetime.now(timezone.utc)
    rules, kw = [], {"FilterExpression": Attr("pk").begins_with("alert#")}
    while True:
        page = client("table").scan(**kw)
        rules += [_plain(i) for i in page["Items"] if i.get("type") == "slo_burn" and i.get("enabled", True)]
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    done = []
    for rule in rules:
        tenant = rule["tenant"]
        slo = _get(f"slo#{tenant}#{rule['slo']}", tenant)
        if not slo:
            continue
        excluded = [_id_of(e) for e in _items(tenant, "exclude#" + tenant + "#")
                    if e.get("check") in slo["checks"] and e.get("expires_at", "") > _now()]
        try:
            s = slo_status(tenant, slo, excluded, now)
        except Exception as e:  # noqa: BLE001  one tenant's failed query mustn't stop the rest
            print(json.dumps({"slo_rule_failed": _id_of(rule), "tenant": tenant, "error": str(e)[:300]}))
            continue
        reasons = []
        if rule.get("burn_rate") is not None and s["burn1h"] is not None and s["burn1h"] >= float(rule["burn_rate"]):
            reasons.append(f"burning its error budget {s['burn1h']:.1f}× too fast over the last hour")
        if rule.get("budget_below") is not None and s["budget_left"] is not None and s["budget_left"] * 100 < float(rule["budget_below"]):
            left = "none" if s["budget_left"] <= 0 else f"{s['budget_left'] * 100:.0f}%"
            reasons.append(f"error budget left: {left} (alert below {float(rule['budget_below']):g}%)")
        rid, subject = _id_of(rule), slo["name"]
        firing = bool(_state(tenant, rid, rule["slo"]).get("firing"))
        link = f"{APP_URL}/#/slos/{rule['slo']}"
        level = "no runs yet" if s["attainment"] is None else f"{s['attainment']:.3f}% good against a {float(slo['target']):g}% target"
        if reasons and not firing:
            fire(tenant, rule, "firing", subject, f"The SLO is {' and '.join(reasons)}. Now: {level}.", link)
            _set_state(tenant, rid, rule["slo"], firing=True)
        elif not reasons and firing:
            fire(tenant, rule, "resolved", subject, f"Back within limits. Now: {level}.", link)
            _set_state(tenant, rid, rule["slo"], firing=False)
        done.append(rid)
    print(json.dumps({"evaluated": len(done)}))
    return {"evaluated": done}


# ------------------------------------------------------------------ API

def handler(event, context=None):
    """One Lambda: API Gateway requests, the runner's {"action": "on_result"}, the schedule."""
    if "httpMethod" in event:
        return api(event, context)
    if event.get("action") == "on_result":
        return on_result(event, context)
    return evaluate(event, context)


def api(event, context=None):
    """/v1/app/alerts/{proxy+}; the tenant is the signed-in user's (Cognito authorizer)."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    tenant, user = claims.get("custom:tenant"), claims.get("email")
    if not tenant or not _TENANT.match(tenant):
        return _http(401, {"error": "no tenant for this user"})
    method = event.get("httpMethod")
    parts = [p for p in ((event.get("pathParameters") or {}).get("proxy") or "").split("/") if p]
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw)
        body = json.loads(raw) if method in ("POST", "PUT") else {}
    except ValueError:
        return _http(400, {"error": "body must be JSON"})
    try:
        return _route(tenant, user, method, parts, body)
    except Refused as e:
        return _http(400, {"error": str(e)})


def _route(tenant, user, method, parts, body):
    kind = parts[0] if parts else ""
    if kind == "channels":
        if len(parts) == 1 and method == "GET":
            return _http(200, {"items": sorted((_public_channel(i) for i in _items(tenant, f"channel#{tenant}#")), key=lambda c: c["name"].lower()),
                               "limit": MAX_CHANNELS})
        if len(parts) == 1 and method == "POST":
            return _http(201, create_channel(tenant, user, body))
        item = _get(f"channel#{tenant}#{parts[1]}", tenant) if len(parts) >= 2 and _ID.match(parts[1]) else None
        if not item:
            return _http(404, {"error": "no such channel"})
        if len(parts) == 2 and method == "DELETE":
            in_use = [r["name"] for r in _items(tenant, f"alert#{tenant}#") if parts[1] in r.get("channels", [])]
            if in_use:
                raise Refused(f"used by: {', '.join(in_use)}; remove it from those rules first")
            delete_channel(tenant, item)
            return _http(200, {"deleted": parts[1]})
        if len(parts) == 3 and parts[2] == "test" and method == "POST":
            test_rule = {"pk": "alert#test#000000000000", "name": "Test alert", "type": "test", "channels": [parts[1]]}
            msg = message(tenant, test_rule, "firing", "a test from Leasyd", "This is a test. Your alerts will arrive here.", f"{APP_URL}/#/alerts")
            error = notify(tenant, item, msg)
            return _http(200, {"sent": error is None, "error": error})
        if len(parts) == 2 and method == "GET":
            return _http(200, _public_channel(item))
    if kind == "rules":
        channels = {_id_of(c) for c in _items(tenant, f"channel#{tenant}#")}
        checks = {_id_of(c) for c in _items(tenant, f"check#{tenant}#")}
        slos = {_id_of(s) for s in _items(tenant, f"slo#{tenant}#")}
        view = lambda it: {**{k: v for k, v in it.items() if k not in ("pk", "tenant")}, "id": _id_of(it)}  # noqa: E731
        if len(parts) == 1 and method == "GET":
            rules = sorted((view(i) for i in _items(tenant, f"alert#{tenant}#")), key=lambda r: r["name"].lower())
            firing = {}
            for s in _items(tenant, f"astate#{tenant}#"):
                if s.get("firing"):
                    firing.setdefault(s["pk"].split("#")[2], []).append(s["pk"].split("#", 3)[3])
            return _http(200, {"items": [{**r, "firing": firing.get(r["id"], [])} for r in rules], "limit": MAX_RULES})
        if len(parts) == 1 and method == "POST":
            rule = validate_rule(body, checks, slos, channels)
            if len(_items(tenant, f"alert#{tenant}#")) >= MAX_RULES:
                raise Refused(f"at most {MAX_RULES} rules")
            item = {"pk": f"alert#{tenant}#{_secrets.token_hex(6)}", "tenant": tenant, **rule, "created_by": user, "created_at": _now()}
            client("table").put_item(Item=_dynamo(item), ConditionExpression="attribute_not_exists(pk)")
            return _http(201, view(item))
        item = _get(f"alert#{tenant}#{parts[1]}", tenant) if len(parts) == 2 and _ID.match(parts[1]) else None
        if not item:
            return _http(404, {"error": "no such rule"})
        if method == "GET":
            return _http(200, view(item))
        if method == "PUT":
            item = {**item, **validate_rule({**view(item), **body}, checks, slos, channels), "updated_at": _now()}
            client("table").put_item(Item=_dynamo(item))
            return _http(200, view(item))
        if method == "DELETE":
            client("table").delete_item(Key={"pk": item["pk"]})
            for s in _items(tenant, f"astate#{tenant}#{parts[1]}#"):
                client("table").delete_item(Key={"pk": s["pk"]})
            return _http(200, {"deleted": parts[1]})
    return _http(404, {"error": "unknown route"})


# ------------------------------------------------------------------ helpers

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, set):
        return sorted(v)
    return str(v)


def _plain(v):
    return json.loads(json.dumps(v, default=_json))


def _dynamo(v):
    from decimal import Decimal
    return json.loads(json.dumps(v), parse_float=Decimal)


def _http(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, default=_json)}
