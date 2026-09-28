import gzip
import json
import os

import duckdb
import pytest

import bloom
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
        assert w["relpath"] == f"dt={w['dt']}/hour={w['hour']}/service={w['service']}/part-b1-000.parquet"


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
    strip = lambda ws: [{**{k: v for k, v in w.items() if k not in ("path", "bloom")},
                         "bloom": w["bloom"].to_bytes()} for w in ws]
    assert strip(w1) == strip(w2)


def test_large_group_split_into_time_ordered_parts(tmp_path):
    f = batch(tmp_path / "a.json.gz", "api", [rec(H20 + n * 10**6) for n in range(25)])
    written = compact.compact_logs([f], str(tmp_path / "o"), "b1", "2026-09-26", "20", max_rows_per_file=10)
    assert [(w["part"], w["rows"]) for w in written] == [(0, 10), (1, 10), (2, 5)]
    assert [w["relpath"].rsplit("/", 1)[1] for w in written] == [
        "part-b1-000.parquet", "part-b1-001.parquet", "part-b1-002.parquet"]
    for a, b in zip(written, written[1:]):
        assert a["max_ts"] <= b["min_ts"]  # consecutive, non-overlapping time slices


def test_bloom_covers_trace_and_request_ids(tmp_path):
    recs = [rec(H20 + n, attrs=[{"key": "request.id", "value": {"stringValue": f"REQ-{n}"}}]) for n in range(50)]
    for n, r in enumerate(recs):
        r["traceId"] = f"{n:032X}"
    f = batch(tmp_path / "a.json.gz", "api", recs)
    [w] = compact.compact_logs([f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    b = w["bloom"]
    assert b.n == 100
    assert all(b.might_contain(bloom.term("trace_id", f"{n:032x}")) for n in range(50))
    assert all(b.might_contain(bloom.term("request.id", f"req-{n}")) for n in range(50))
    assert not b.might_contain(bloom.term("trace_id", "f" * 32))


def test_bloom_per_part_only_holds_that_parts_ids(tmp_path):
    recs = [rec(H20 + n * 10**6) for n in range(20)]
    for n, r in enumerate(recs):
        r["traceId"] = f"{n:032x}"
    f = batch(tmp_path / "a.json.gz", "api", recs)
    w0, w1 = compact.compact_logs([f], str(tmp_path / "o"), "b1", "2026-09-26", "20", max_rows_per_file=10)
    assert w0["bloom"].might_contain(bloom.term("trace_id", f"{3:032x}"))
    assert not w1["bloom"].might_contain(bloom.term("trace_id", f"{3:032x}"))
    assert w1["bloom"].might_contain(bloom.term("trace_id", f"{13:032x}"))


def test_collector_routing_labels_not_stored(tmp_path):
    path = tmp_path / "a.json.gz"
    doc = {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "api"}},
        {"key": "obs.tenant", "value": {"stringValue": "acme"}},
        {"key": "obs.s3_prefix", "value": {"stringValue": "_incoming/tenant=acme/logs"}},
        {"key": "host.name", "value": {"stringValue": "h1"}}]},
        "scopeLogs": [{"scope": {}, "logRecords": [rec(H20)]}]}]}
    with gzip.open(path, "wt") as f:
        f.write(json.dumps(doc))
    [w], _ = run(tmp_path, [str(path)])
    [(m,)] = read(w["path"], "SELECT resource_attributes FROM t")
    assert m == {"service.name": "api", "host.name": "h1"}


def test_summarize_matches_what_compaction_writes(tmp_path):
    recs = [rec(H20 + n * 10**9) for n in range(30)] + [rec(H20 + 3600 * 10**9 + 5)]
    for n, r in enumerate(recs):
        r["traceId"] = f"{n:032x}"
    f = batch(tmp_path / "a.json.gz", "api", recs)
    summary = compact.summarize_logs([f], str(tmp_path / "s"), "2026-09-26", "20")
    written = compact.compact_logs([f], str(tmp_path / "o"), "b1", "2026-09-26", "20")
    key = lambda d: (d["service"], d["dt"], d["hour"], d["rows"], d["min_ts"], d["max_ts"])
    assert [key(d) for d in summary] == [key(w) for w in written]
    assert [d["bloom"].to_bytes() for d in summary] == [w["bloom"].to_bytes() for w in written]
    assert not os.path.exists(tmp_path / "s" / "dt=2026-09-26")  # nothing written


def test_spill_dir_exists_before_duckdb_needs_it(tmp_path):
    """A chunk bigger than the memory limit spills to <out>/.duckdb_tmp. DuckDB creates that
    folder but not its parents, and <out> doesn't exist yet (every big chunk failed on AWS)."""
    out = tmp_path / "not" / "yet" / "there"
    con = compact._connect(str(out), "100MB")
    try:
        assert (out / ".duckdb_tmp").is_dir()
        assert con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(out / ".duckdb_tmp")
    finally:
        con.close()


def _changeover_lines():
    lines = []
    for svc in ("api", "web"):
        recs = [rec(H20 + n * 10**6, body=f"msg {svc} {n}", observed=H20 + n,
                    attrs=[{"key": "request.id", "value": {"stringValue": f"r{svc}{n}"}},
                           {"key": "n", "value": {"intValue": str(n)}}]) for n in range(40)]
        for n, r in enumerate(recs):
            r["traceId"] = f"{n:032x}"
        doc = {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                                 "scopeLogs": [{"scope": {"name": "lib"}, "logRecords": recs}]}]}
        lines.append((json.dumps(doc) + "\n").encode())
    return lines


def _encode(lines, encoding):
    raw = b"".join(lines)
    return {"one_gzip": gzip.compress(raw), "gzip_members": b"".join(gzip.compress(line) for line in lines),
            "plain": raw, "gzip_in_gzip": gzip.compress(b"".join(gzip.compress(line) for line in lines))}[encoding]


def _compact_encoded(tmp_path, encoding):
    path = tmp_path / encoding / "raw.json.gz"
    path.parent.mkdir()
    body = _encode(_changeover_lines(), encoding)
    path.write_bytes(body)
    written = compact.compact_logs([str(path)], str(tmp_path / encoding / "out"), "b1", "2026-09-26", "20")
    assert path.read_bytes() == body   # the original is left as it was
    content = {w["service"]: read(w["path"], "SELECT * FROM t ORDER BY ts_unix_nano") for w in written}
    blooms = {w["service"]: w["bloom"].to_bytes() for w in written}
    return content, blooms


def test_raw_file_encodings_from_the_compression_changeover(tmp_path):
    """Firehose-compressed (old), per-record gzip members (new), plain, and double gzip all
    compact to exactly the same Parquet: every column of every row, and the same blooms."""
    base, base_blooms = _compact_encoded(tmp_path, "one_gzip")
    assert sorted((s, len(r)) for s, r in base.items()) == [("api", 40), ("web", 40)]
    for encoding in ("gzip_members", "plain", "gzip_in_gzip"):
        content, blooms = _compact_encoded(tmp_path, encoding)
        assert content == base, encoding
        assert blooms == base_blooms, encoding


def test_batched_id_digests_match_one_at_a_time(tmp_path):
    """The day/hour filter digests from the batched code equal a plain per-ID loop."""
    import dayfilter
    recs = [rec(H20 + n * 60 * 10**9, attrs=[{"key": "request.id", "value": {"stringValue": f"R{n % 97}"}}])
            for n in range(150)]                      # spans hours 20-22, repeats request ids
    for n, r in enumerate(recs):
        r["traceId"] = f"{n:032x}"
    f = batch(tmp_path / "a.json.gz", "api", recs)
    got = {}
    compact.compact_logs([f], str(tmp_path / "o"), "b1", "2026-09-26", "20", id_digests=got)
    want = {}
    for n, r in enumerate(recs):
        hour = (20 + n // 60)
        for t in {f"trace_id={n:032x}", f"request.id=r{n % 97}"}:
            want.setdefault(("2026-09-26", f"{hour:02d}"), set()).add(t)
    expect = {}
    for key, terms in want.items():
        for t in terms:
            d = dayfilter.digest(t)
            expect.setdefault(key, {}).setdefault(dayfilter.group_of(d), set()).add(d)
    as_sets = {k: {g: {bytes(b[i:i + 12]) for i in range(0, len(b), 12)} for g, b in v.items()} for k, v in got.items()}
    assert as_sets == expect
    assert all(len(b) % 12 == 0 and len(b) // 12 == len(as_sets[k][g])   # no duplicates within an hour
               for k, v in got.items() for g, b in v.items())


def test_malformed_values_do_not_fail_the_chunk(tmp_path):
    """A client can send a body or attribute value that isn't an OTLP AnyValue object.
    Such a record must not make the whole chunk fail (it would never compact)."""
    r = rec(H20, attrs=[{"key": "n", "value": 5}, {"key": "s", "value": "text"}, {"key": "ok", "value": {"stringValue": "v"}}])
    r["body"] = "plain body"
    f = batch(tmp_path / "a.json.gz", "api", [r, rec(H20 + 1)])
    [w], _ = run(tmp_path, [f])
    rows = read(w["path"], "SELECT body, attributes FROM t ORDER BY ts_unix_nano")
    assert rows[0] == ('"plain body"', {"n": "5", "s": '"text"', "ok": "v"})
    assert rows[1][0] == "msg"
