import gzip
import json
import os

import duckdb
import pytest

import compact

H20 = 1790452800 * 10**9  # 2026-09-26T20:00:00Z in ns


def rec(ts_ns, body="msg", attrs=None, observed=None):
    r = {
        "timeUnixNano": str(ts_ns),
        "severityNumber": 9,
        "severityText": "Info",
        "body": {"stringValue": body},
        "attributes": attrs or [],
    }
    if observed is not None:
        r["observedTimeUnixNano"] = str(observed)
    return r


def batch(path, service, records):
    res = {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]} if service else {}
    doc = {"resourceLogs": [{"resource": res, "scopeLogs": [{"scope": {"name": "s"}, "logRecords": records}]}]}
    with gzip.open(path, "wt") as f:
        f.write(json.dumps(doc))  # the collector writes no trailing newline
    return str(path)


def run(tmp_path, files):
    out = tmp_path / "out"
    return compact.compact_logs(files, str(out), "b1", "2026-09-26", "20"), out


def read(path, sql="SELECT * FROM t"):
    con = duckdb.connect()
    con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{path}')")
    return con.execute(sql).fetchall()


def test_splits_by_service_and_event_hour(tmp_path):
    f1 = batch(tmp_path / "a.json.gz", "api", [rec(H20 + 59 * 60 * 10**9), rec(H20 + 61 * 60 * 10**9)])
    f2 = batch(tmp_path / "b.json.gz", "web", [rec(H20 + 5)])
    written, _ = run(tmp_path, [f1, f2])
    got = sorted((w["service"], w["dt"], w["hour"], w["rows"]) for w in written)
    assert got == [("api", "2026-09-26", "20", 1), ("api", "2026-09-26", "21", 1), ("web", "2026-09-26", "20", 1)]
    for w in written:
        assert w["min_ts"][:13] == w["max_ts"][:13]  # never spans an hour
        assert w["relpath"] == f"dt={w['dt']}/hour={w['hour']}/service={w['service']}/part-b1.parquet"


def test_rows_sorted_and_counts_match(tmp_path):
    ts = [H20 + n * 10**6 for n in (5, 1, 9, 3)]
    f = batch(tmp_path / "a.json.gz", "api", [rec(t) for t in ts])
    [w], _ = run(tmp_path, [f])
    assert w["rows"] == 4
    assert [r[0] for r in read(w["path"], "SELECT ts_unix_nano FROM t")] == sorted(ts)
    assert w["min_ts"] == "2026-09-26T20:00:00.001000Z"
    assert w["max_ts"] == "2026-09-26T20:00:00.009000Z"


def test_attribute_types_and_duplicate_keys(tmp_path):
    attrs = [
        {"key": "s", "value": {"stringValue": "x"}},
        {"key": "i", "value": {"intValue": "42"}},
        {"key": "d", "value": {"doubleValue": 1.5}},
        {"key": "b", "value": {"boolValue": True}},
        {"key": "s", "value": {"stringValue": "dup"}},
    ]
    f = batch(tmp_path / "a.json.gz", "api", [rec(H20, attrs=attrs)])
    [w], _ = run(tmp_path, [f])
    [(m,)] = read(w["path"], "SELECT attributes FROM t")
    assert m == {"s": "x", "i": "42", "d": "1.5", "b": "true"}


def test_missing_timestamp_falls_back(tmp_path):
    f = batch(tmp_path / "a.json.gz", "api", [rec(0, observed=H20 + 7), rec(0)])
    [w], _ = run(tmp_path, [f])
    assert sorted(r[0] for r in read(w["path"], "SELECT ts_unix_nano FROM t")) == [H20, H20 + 7]


def test_missing_service_is_unknown(tmp_path):
    f = batch(tmp_path / "a.json.gz", None, [rec(H20)])
    [w], _ = run(tmp_path, [f])
    assert w["service"] == "unknown"


def test_service_name_is_path_safe():
    assert compact.safe_service("a/b c=d") == "a_b_c_d"
    assert compact.safe_service("") == "unknown"


def test_same_inputs_same_outputs(tmp_path):
    f = batch(tmp_path / "a.json.gz", "api", [rec(H20 + n) for n in range(100)])
    w1 = compact.compact_logs([f], str(tmp_path / "o1"), "b1", "2026-09-26", "20")
    w2 = compact.compact_logs([f], str(tmp_path / "o2"), "b1", "2026-09-26", "20")
    strip = lambda ws: [{k: v for k, v in w.items() if k != "path"} for w in ws]
    assert strip(w1) == strip(w2)
