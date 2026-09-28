import dayfilter as df


def build(terms):
    ds = [df.digest(t) for t in terms]
    by_group = {}
    for d in ds:
        by_group.setdefault(df.group_of(d), bytearray()).extend(d)
    return {g: df.build_group(bytes(buf)) for g, buf in by_group.items()}


def check(groups, term):
    d = df.digest(term)
    g = groups.get(df.group_of(d))
    if g is None:
        return False
    s, m, _, bits = g
    lo, hi = df.byte_range(d, s, m)
    return df.might_contain(bits[lo:hi + 1], d, s, m)


def test_no_false_negatives_and_about_1pct_false_positives():
    terms = [f"trace_id={i:032x}" for i in range(300_000)]
    groups = build(terms + terms[:1000])            # duplicates are counted once
    assert sum(g[2] for g in groups.values()) == 300_000
    assert all(check(groups, t) for t in terms[::97])
    fp = sum(check(groups, f"trace_id={i:032x}") for i in range(10**9, 10**9 + 20_000)) / 20_000
    assert fp < 0.015
    s, m, n, bits = groups[0]
    assert len(bits) == s * m // 8


def test_geometry_grows_subshards_not_reads():
    assert df.geometry(0) == (1, 64)
    s, m = df.geometry(100_000_000)
    assert s == df.MAX_SUBSHARDS
    s1, m1 = df.geometry(1_000_000)
    assert s1 == 32 and m1 // 8 < 80_000        # ~60 KB per lookup per day


def test_empty_group():
    s, m, n, bits = df.build_group(b"")
    assert (s, n, len(bits)) == (1, 0, m // 8)
