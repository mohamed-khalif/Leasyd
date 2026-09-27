"""Bloom filters for file-level pruning of ID lookups.

One filter per Parquet file holds every value of the indexed fields in that
file, as "field=value" strings (e.g. "trace_id=4bf9...", "request.id=abc").
It answers "could this file contain X?" with a definite no for almost every
file that doesn't, so an ID lookup opens one or two files instead of all.

Pure Python, no dependencies: used by the compaction worker (build) and the
index lookup (check).
"""

import hashlib
import math

DEFAULT_FPP = 0.01  # 1% false positives: ~9.6 bits per distinct value


def term(field, value):
    """The string stored for a field value. IDs are hex; case-fold them."""
    return f"{field}={str(value).strip().lower()}"


class Bloom:
    def __init__(self, m_bits, k, bits=None):
        self.m = m_bits
        self.k = k
        self.bits = bytearray(bits) if bits is not None else bytearray((m_bits + 7) // 8)

    @classmethod
    def for_capacity(cls, n, fpp=DEFAULT_FPP):
        n = max(n, 1)
        m = max(64, math.ceil(-n * math.log(fpp) / (math.log(2) ** 2)))
        m = (m + 7) // 8 * 8
        k = max(1, round(m / n * math.log(2)))
        return cls(m, k)

    @classmethod
    def build(cls, terms, fpp=DEFAULT_FPP):
        terms = list(terms)
        b = cls.for_capacity(len(terms), fpp)
        for t in terms:
            b.add(t)
        return b

    def _positions(self, item):
        d = hashlib.blake2b(item.encode(), digest_size=16).digest()
        h1 = int.from_bytes(d[:8], "little")
        h2 = int.from_bytes(d[8:], "little") | 1
        for i in range(self.k):
            yield (h1 + i * h2) % self.m

    def add(self, item):
        for p in self._positions(item):
            self.bits[p >> 3] |= 1 << (p & 7)

    def might_contain(self, item):
        return all(self.bits[p >> 3] & (1 << (p & 7)) for p in self._positions(item))

    def to_bytes(self):
        return bytes(self.bits)
