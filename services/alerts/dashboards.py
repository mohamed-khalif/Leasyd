"""Dashboards (D2): a tenant's saved dashboards, panels of PromQL queries over logs, spans and
metrics. Served by obs-alerts (the tenant's settings) at /v1/app/dashboards[/{id}].

  dash#<tenant>#<id>   name, description, variables (labels to filter every panel by, e.g.
                       service_name: the panels' PromQL uses $service_name), panels, version.
                       A panel: type (timeseries | bars | stat | text), title, description, queries
                       ([{promql, legend}]), unit, decimals, w (1-12 columns), h (1-6 rows), text.

Every user of the tenant sees and edits the same dashboards. A save names the version it was
edited from; if someone saved in between, it's refused (409) instead of overwriting their work.
"""
import json
import re
import secrets as _secrets
from datetime import datetime, timezone

MAX_DASHBOARDS, MAX_PANELS, MAX_QUERIES, MAX_VARIABLES = 100, 60, 5, 5
MAX_BYTES = 300_000          # a DynamoDB item holds 400 KB
PANEL_TYPES = {"timeseries", "bars", "stat", "text"}
UNITS = {"", "ms", "s", "ns", "bytes", "%", "/s", "percentunit"}
_ID = re.compile(r"^[a-z0-9]{12}$")
_PANEL_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")


class Invalid(ValueError):
    pass


def _text(v, what, lo, hi):
    s = "" if v is None else str(v).strip()
    if not lo <= len(s) <= hi:
        raise Invalid(f"{what}: {lo}-{hi} characters")
    return s


def _int(v, what, lo, hi, default):
    try:
        n = int(v if v not in (None, "") else default)
    except (TypeError, ValueError):
        raise Invalid(f"{what}: a whole number")
    if not lo <= n <= hi:
        raise Invalid(f"{what}: {lo}-{hi}")
    return n


def validate(body):
    """A dashboard as the API accepts it -> the stored fields (without id, tenant, version)."""
    if not isinstance(body, dict):
        raise Invalid("expected a JSON object")
    out = {"name": _text(body.get("name"), "name", 1, 100), "description": _text(body.get("description"), "description", 0, 500)}
    variables = body.get("variables") or []
    if not isinstance(variables, list) or len(variables) > MAX_VARIABLES:
        raise Invalid(f"variables: at most {MAX_VARIABLES}")
    out["variables"] = []
    for i, v in enumerate(variables):
        if not isinstance(v, dict) or not _LABEL.match(str(v.get("name", ""))):
            raise Invalid(f"variable {i + 1}: a label name such as service_name")
        out["variables"].append({"name": v["name"], "label": _text(v.get("label") or v["name"], f"variable {i + 1} label", 1, 60)})
    panels = body.get("panels") or []
    if not isinstance(panels, list) or len(panels) > MAX_PANELS:
        raise Invalid(f"panels: at most {MAX_PANELS}")
    out["panels"], seen = [], set()
    for i, p in enumerate(panels):
        where = f"panel {i + 1}"
        if not isinstance(p, dict):
            raise Invalid(f"{where}: expected an object")
        pid = str(p.get("id") or _secrets.token_hex(4))
        if not _PANEL_ID.match(pid) or pid in seen:
            raise Invalid(f"{where}: bad or repeated id")
        seen.add(pid)
        kind = p.get("type", "timeseries")
        if kind not in PANEL_TYPES:
            raise Invalid(f"{where}: type is one of {sorted(PANEL_TYPES)}")
        panel = {"id": pid, "type": kind, "title": _text(p.get("title"), f"{where} title", 1, 120),
                 "description": _text(p.get("description"), f"{where} description", 0, 500),
                 "w": _int(p.get("w"), f"{where} width", 1, 12, 6), "h": _int(p.get("h"), f"{where} height", 1, 6, 2)}
        if kind == "text":
            panel["text"] = _text(p.get("text"), f"{where} text", 0, 5000)
        else:
            queries = p.get("queries") or []
            if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
                raise Invalid(f"{where}: 1-{MAX_QUERIES} queries")
            panel["queries"] = [{"promql": _text(q.get("promql") if isinstance(q, dict) else None, f"{where} query {j + 1}", 1, 2000),
                                 "legend": _text(q.get("legend") if isinstance(q, dict) else "", f"{where} legend {j + 1}", 0, 200)}
                                for j, q in enumerate(queries)]
            unit = p.get("unit") or ""
            if unit not in UNITS:
                raise Invalid(f"{where}: unit is one of {sorted(UNITS)}")
            panel["unit"] = unit
            if p.get("decimals") not in (None, ""):
                panel["decimals"] = _int(p["decimals"], f"{where} decimals", 0, 6, 2)
        out["panels"].append(panel)
    if len(json.dumps(out)) > MAX_BYTES:
        raise Invalid("the dashboard is too large; split it in two")
    return out


def summary(item):
    return {"id": item["pk"].split("#")[2], "name": item["name"], "description": item.get("description", ""),
            "panels": len(item.get("panels") or []), "version": int(item.get("version", 1)),
            "updated_at": item.get("updated_at"), "updated_by": item.get("updated_by")}


def view(item):
    return {**{k: v for k, v in item.items() if k not in ("pk", "tenant")}, "id": item["pk"].split("#")[2],
            "version": int(item.get("version", 1))}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def api(table, items, tenant, user, method, dash_id, body, plain, dynamo, http):
    """GET / POST /v1/app/dashboards, GET / PUT / DELETE /v1/app/dashboards/{id}.
    table: the obs-tenants Table; items(tenant, prefix) lists a tenant's items; plain/dynamo convert
    numbers; http(status, body) answers."""
    from boto3.dynamodb.conditions import Attr
    from botocore.exceptions import ClientError
    try:
        if dash_id is None:
            if method == "GET":
                return http(200, {"items": sorted((summary(plain(i)) for i in items(tenant, f"dash#{tenant}#")),
                                                  key=lambda d: d["name"].lower()), "limit": MAX_DASHBOARDS})
            if method == "POST":
                if len(items(tenant, f"dash#{tenant}#")) >= MAX_DASHBOARDS:
                    raise Invalid(f"at most {MAX_DASHBOARDS} dashboards")
                item = {"pk": f"dash#{tenant}#{_secrets.token_hex(6)}", "tenant": tenant, **validate(body), "version": 1,
                        "created_by": user, "created_at": _now(), "updated_by": user, "updated_at": _now()}
                table.put_item(Item=dynamo(item), ConditionExpression="attribute_not_exists(pk)")
                return http(201, view(item))
            return http(405, {"error": "method not allowed"})
        if not _ID.match(dash_id):
            return http(404, {"error": "no such dashboard"})
        key = {"pk": f"dash#{tenant}#{dash_id}"}
        item = table.get_item(Key=key).get("Item")
        if not item or item.get("tenant") != tenant:
            return http(404, {"error": "no such dashboard"})
        item = plain(item)
        if method == "GET":
            return http(200, view(item))
        if method == "DELETE":
            table.delete_item(Key=key)
            return http(200, {"deleted": dash_id})
        if method == "PUT":
            base = int(body.get("version") or 0) if isinstance(body, dict) else 0
            if base != int(item.get("version", 1)):
                return http(409, {"error": f"{item.get('updated_by') or 'someone'} saved this dashboard since you opened it "
                                           f"(version {item.get('version', 1)}); reload it to see their changes",
                                  "version": int(item.get("version", 1))})
            new = {**{k: item[k] for k in ("pk", "tenant", "created_by", "created_at") if k in item}, **validate({**view(item), **body}),
                   "version": base + 1, "updated_by": user, "updated_at": _now()}
            try:
                table.put_item(Item=dynamo(new), ConditionExpression=Attr("version").eq(base))
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    return http(409, {"error": "someone saved this dashboard at the same moment; reload it"})
                raise
            return http(200, view(new))
        return http(405, {"error": "method not allowed"})
    except Invalid as e:
        return http(400, {"error": str(e)})
