import random

from bloom import Bloom, term


def test_no_false_negatives():
    terms = [term("trace_id", f"{random.getrandbits(128):032x}") for _ in range(5000)]
    b = Bloom.build(terms)
    assert all(b.might_contain(t) for t in terms)


def test_false_positive_rate_near_target():
    rnd = random.Random(1)
    b = Bloom.build(term("trace_id", f"{rnd.getrandbits(128):032x}") for _ in range(20000))
    probes = [term("trace_id", f"{rnd.getrandbits(128):032x}") for _ in range(20000)]
    fp = sum(b.might_contain(t) for t in probes) / len(probes)
    assert fp < 0.02


def test_round_trips_through_bytes():
    b = Bloom.build([term("request.id", "abc")])
    c = Bloom(b.m, b.k, b.to_bytes())
    assert c.might_contain(term("request.id", "ABC"))  # case-folded
    assert not c.might_contain(term("request.id", "abd"))


def test_size_is_about_ten_bits_per_value():
    b = Bloom.build(term("trace_id", str(i)) for i in range(100_000))
    assert 110_000 < len(b.to_bytes()) < 130_000
    assert b.k == 7


def test_empty_filter_matches_nothing():
    b = Bloom.build([])
    assert not b.might_contain(term("trace_id", "x"))


def test_add_many_is_bit_identical_to_add():
    import random
    terms = [f"trace_id={random.getrandbits(128):032x}" for _ in range(20_000)] + ["request.id=x", ""]
    for m_items in (1, 500, 20_000):
        one, many = Bloom.for_capacity(m_items), Bloom.for_capacity(m_items)
        for t in terms:
            one.add(t)
        many.add_many(terms[:7000])
        many.add_many(terms[7000:])
        many.add_many([])
        assert one.to_bytes() == many.to_bytes()
