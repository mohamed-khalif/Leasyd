"""What a read-only SQL question needs from each table, for the scan fan-out (docs/bench/sql-classification.md).

Many workers read the files in parallel, each keeping only the columns the question uses and only rows
that can matter to it; the question's own SQL then runs unchanged over those rows. So the answer is
exact as long as, for each table:
  - columns: every name the query uses (the workers keep those the files have: a superset is fine);
  - row filter: true for every row any reference to the table keeps (a superset of rows is fine).

The row filter of a table is the OR, over every place the query reads it (FROM logs, a CTE's FROM
metrics, a subquery's FROM spans ...), of the AND of that place's own WHERE conditions of a simple
form: col = constant, col IN (constants), map['key'] = constant, ... IS NOT NULL, and ORs of those,
on the table's own columns. Leaving a condition out only keeps more rows, so anything else in a
WHERE is simply not used; a place with no usable condition (or read through a JOIN) means the table
gets no row filter at all.

plan(sql) -> {table: {"columns": [...] or None (all), "filter": SQL text or None, "services": [...] or None}},
or None when the query can't be analysed (the caller then runs it the single-worker way)."""
import json

import duckdb

# The tables' real columns (Parquet), by SQL table name. A name outside these is a CTE's or an alias's.
MAP_COLUMNS = {"attributes", "resource_attributes"}
_COMMON = {"ts", "ts_unix_nano", "service", "scope_name", "scope_version", *MAP_COLUMNS}
TABLE_COLUMNS = {
    "logs": _COMMON | {"observed_ts", "severity_number", "severity_text", "body", "trace_id", "span_id", "flags",
                       "event_name"},
    "spans": _COMMON | {"end_ts", "duration_ns", "name", "kind", "status_code", "status_message", "trace_id",
                        "span_id", "parent_span_id", "trace_state", "events", "links", "session_id",
                        "gen_ai_operation", "gen_ai_model", "gen_ai_agent", "gen_ai_tool", "gen_ai_input_tokens",
                        "gen_ai_output_tokens", "gen_ai_cost", "gen_ai_conversation_id", "gen_ai_provider"},
    "metrics": _COMMON | {"start_ts", "metric_name", "metric_type", "unit", "description", "temporality",
                          "is_monotonic", "value", "count", "sum", "min", "max", "flags", "bucket_counts",
                          "explicit_bounds", "quantiles", "scale", "zero_count", "positive", "negative", "exemplars"},
}
_INTEGER_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT"}
_SERVICE_LIMIT = 50


class _NoPlan(Exception):
    """The query has a shape this module doesn't analyse: run it the single-worker way."""


def plan(sql):
    con = duckdb.connect()
    try:
        con.execute("SET enable_external_access=false")
        tree = json.loads(con.execute("SELECT json_serialize_sql(?)::VARCHAR", [sql]).fetchone()[0])
    finally:
        con.close()
    if tree.get("error") or len(tree.get("statements") or []) != 1:
        return None
    reads = {t: [] for t in TABLE_COLUMNS}   # table -> per place it is read: (conditions, services) or None
    names, star, seen = set(), [False], [0]
    try:
        _walk(tree["statements"][0]["node"], frozenset(), reads, names, star, seen)
    except (_NoPlan, KeyError, TypeError, RecursionError):
        return None
    if seen[0] != sum(len(p) for p in reads.values()):
        return None   # a table read somewhere this module doesn't place (e.g. PIVOT): no plan
    out = {}
    for table, places in reads.items():
        if not places:
            continue
        filters = None if any(p is None or not p[0] for p in places) else \
            " OR ".join("(" + " AND ".join(p[0]) + ")" for p in places)
        services = None
        if all(p is not None and p[1] is not None for p in places):
            services = sorted({s for p in places for s in p[1]})
            if not services or len(services) > _SERVICE_LIMIT:
                services = None
        columns = None if star[0] else sorted(names | {"ts"})
        out[table] = {"columns": columns, "filter": filters, "services": services}
    return out or None


def _walk(n, ctes, reads, names, star, seen):
    """Collects every name used in a column reference, any *, and for each SELECT reading a table
    directly, that read's usable WHERE conditions. ctes: the CTE names visible here (they hide a table
    of the same name). seen: tables read, counted to check every read was placed."""
    if isinstance(n, list):
        for v in n:
            _walk(v, ctes, reads, names, star, seen)
        return
    if not isinstance(n, dict):
        return
    if isinstance(n.get("cte_map"), dict):
        ctes = ctes | {str(e.get("key", "")).lower() for e in n["cte_map"].get("map") or []}
    cls = n.get("class")
    if cls == "COLUMN_REF":
        names.update(str(p).lower() for p in n["column_names"])   # every part: a.b may be table.column or column.field
    elif cls == "STAR":
        star[0] = True
    elif n.get("type") == "TABLE_FUNCTION":
        raise _NoPlan()
    elif n.get("type") == "BASE_TABLE" and _table_of(n, ctes):
        seen[0] += 1
    if n.get("type") == "SELECT_NODE":
        frm = n.get("from_table") or {}
        direct = _table_of(frm, ctes)
        if direct:
            refs = {direct, str(frm.get("alias") or "").lower()} - {""}
            reads[direct].append(_conditions(n.get("where_clause"), direct, refs))
        _mark_other_reads(frm, ctes, reads, skip=frm if direct else None)
    for v in n.values():
        _walk(v, ctes, reads, names, star, seen)


def _table_of(node, ctes):
    """The table a FROM item reads directly, or None (a CTE, a join, a subquery ...)."""
    if isinstance(node, dict) and node.get("type") == "BASE_TABLE":
        if node.get("schema_name") or node.get("catalog_name"):
            raise _NoPlan()
        name = str(node.get("table_name", "")).lower()
        if name not in ctes:
            if name not in TABLE_COLUMNS:
                raise _NoPlan()
            return name
    return None


def _mark_other_reads(node, ctes, reads, skip):
    """A table read inside a JOIN (or anywhere but a SELECT's own FROM) keeps all its rows: its WHERE
    applies to the joined rows, not to the table's."""
    if not isinstance(node, dict) or node is skip:
        return
    if node.get("type") == "JOIN":
        for side in ("left", "right"):
            t = _table_of(node.get(side), ctes)
            if t:
                reads[t].append(None)
            else:
                _mark_other_reads(node.get(side), ctes, reads, None)


def _conditions(where, table, refs):
    """-> (the usable conditions as SQL, the services they limit to or None)."""
    if where is None:
        return ([], None)
    usable, services = [], None
    for c in _and_parts(where):
        sql = _simple(c, table, refs)
        if sql is None:
            continue
        usable.append(sql)
        s = _services_of(c, table, refs)
        if s is not None:
            services = s if services is None else sorted(set(services) & set(s))
    return (usable, services)


def _and_parts(n):
    if n.get("class") == "CONJUNCTION" and n.get("type") == "CONJUNCTION_AND":
        return [p for c in n["children"] for p in _and_parts(c)]
    return [n]


def _simple(n, table, refs):
    """SQL for a condition of a simple form on table's own columns, else None."""
    t, cls = n.get("type"), n.get("class")
    if cls == "CONJUNCTION" and t in ("CONJUNCTION_AND", "CONJUNCTION_OR"):
        parts = [_simple(c, table, refs) for c in n["children"]]
        if any(p is None for p in parts):
            return None
        return "(" + (" AND " if t == "CONJUNCTION_AND" else " OR ").join(parts) + ")"
    if cls == "COMPARISON" and t == "COMPARE_EQUAL":
        a, b = _operand(n["left"], table, refs), _constant(n["right"])
        if a is None or b is None:
            a, b = _operand(n["right"], table, refs), _constant(n["left"])
        return f"{a} = {b}" if a is not None and b is not None else None
    if cls == "OPERATOR" and t == "COMPARE_IN":
        a = _operand(n["children"][0], table, refs)
        values = [_constant(c) for c in n["children"][1:]]
        if a is None or not values or any(v is None for v in values):
            return None
        return f"{a} IN ({', '.join(values)})"
    if cls == "OPERATOR" and t == "OPERATOR_IS_NOT_NULL":
        a = _operand(n["children"][0], table, refs)
        return f"{a} IS NOT NULL" if a is not None else None
    return None


def _operand(n, table, refs):
    """A column of the table (qualified by its name or alias, or not at all) or map['key'] of one."""
    if n.get("class") == "COLUMN_REF":
        parts = [str(p).lower() for p in n["column_names"]]
        if len(parts) == 2 and parts[0] not in refs or len(parts) > 2:
            return None
        if parts[-1] not in TABLE_COLUMNS[table]:
            return None
        return '"' + parts[-1] + '"'
    if n.get("class") == "OPERATOR" and n.get("type") == "ARRAY_EXTRACT" and len(n["children"]) == 2:
        col, key = _operand(n["children"][0], table, refs), n["children"][1]
        if col is None or col.strip('"') not in MAP_COLUMNS:
            return None
        k = _constant(key)
        return f"{col}[{k}]" if k is not None and k.startswith("'") else None
    return None


def _constant(n):
    if n.get("class") != "CONSTANT":
        return None
    v = n["value"]
    if v.get("is_null"):
        return None
    kind, value = v["type"]["id"], v["value"]
    if kind == "VARCHAR" and isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if kind in _INTEGER_TYPES and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if kind == "BOOLEAN" and isinstance(value, bool):
        return "true" if value else "false"
    return None


def _services_of(n, table, refs):
    """The services a condition limits the rows to (service = 'x' or service IN (...)), or None."""
    if n.get("class") == "COMPARISON" and n.get("type") == "COMPARE_EQUAL":
        for col, val in ((n["left"], n["right"]), (n["right"], n["left"])):
            if _operand(col, table, refs) == '"service"' and _constant(val) and _constant(val).startswith("'"):
                return [val["value"]["value"]]
    if n.get("class") == "OPERATOR" and n.get("type") == "COMPARE_IN" \
            and _operand(n["children"][0], table, refs) == '"service"':
        vals = [c["value"]["value"] for c in n["children"][1:] if (_constant(c) or "").startswith("'")]
        if len(vals) == len(n["children"]) - 1:
            return vals
    return None
