"""Per-day ID filters: one bloom filter per (tenant, signal, event day)
covering every ID (trace_id and the bloom attributes) in that day's
Parquet, so an ID lookup across 30 days checks 30 small pieces instead of
every file's bloom.

A day filter is split into 16 group files, each split into up to 256
equal sub-shards; an ID's digest picks its group and sub-shard. A lookup
therefore reads one range of one file per day (tens of KB), however big the
day is. Each sub-shard is sized for its share of the day's IDs at a 1%
false-positive rate.

Built by the sealer from the 12-byte digests compaction writes per chunk
(see handler.py), with numpy, since a busy day holds hundreds of millions
of IDs.

Digest layout (blake2b, 12 bytes):
  bytes 0-3  h1, bytes 4-7 h2   bit positions (h1 + i*h2) mod m, i < K
  byte 8     top 4 bits: group
  bytes 9-10 sub-shard (low bits)
"""

import hashlib
import math

K = 7
GROUPS = 16
DIGEST_BYTES = 12
BITS_PER_ID = 9.6          # 1% false positives at K = 7
MARGIN = 1.25              # sub-shards get uneven shares of IDs
IDS_PER_SUBSHARD = 50_000  # ~60 KB read per lookup per day
MAX_SUBSHARDS = 256


def digest(term):
    return hashlib.blake2b(term.encode(), digest_size=DIGEST_BYTES).digest()


def group_of(d):
    return d[8] >> 4


def geometry(n_ids):
    """(sub-shards, bits per sub-shard) for a group holding n_ids IDs."""
    subshards = 1
    while subshards < MAX_SUBSHARDS and n_ids / subshards > IDS_PER_SUBSHARD:
        subshards *= 2
    m = max(64, math.ceil(n_ids / subshards * MARGIN * BITS_PER_ID / 64) * 64)
    return subshards, m


def _parts(d, subshards):
    h1 = int.from_bytes(d[0:4], "little")
    h2 = int.from_bytes(d[4:8], "little") | 1
    sub = int.from_bytes(d[9:11], "little") & (subshards - 1)
    return h1, h2, sub


def build_group(digests):
    """digests: bytes, a concatenation of 12-byte digests of one group (may
    repeat). Returns (sub-shards, m, n distinct IDs, filter bytes). The
    filter is the sub-shards' bit arrays back to back, m/8 bytes each."""
    import numpy as np

    a = np.frombuffer(digests, dtype=np.uint8).reshape(-1, DIGEST_BYTES)
    if len(a):
        a = np.unique(np.ascontiguousarray(a).view(f"V{DIGEST_BYTES}")).view(np.uint8).reshape(-1, DIGEST_BYTES)
    n = len(a)
    subshards, m = geometry(n)
    bits = np.zeros(subshards * m, dtype=bool)
    if n:
        h1 = np.ascontiguousarray(a[:, 0:4]).view("<u4").ravel().astype(np.uint64)
        h2 = (np.ascontiguousarray(a[:, 4:8]).view("<u4").ravel() | 1).astype(np.uint64)
        sub = (np.ascontiguousarray(a[:, 9:11]).view("<u2").ravel() & (subshards - 1)).astype(np.uint64)
        base = sub * np.uint64(m)
        for i in range(K):
            bits[base + (h1 + np.uint64(i) * h2) % np.uint64(m)] = True
    return subshards, m, n, np.packbits(bits, bitorder="little").tobytes()


def byte_range(d, subshards, m):
    """(first byte, last byte) of the sub-shard holding digest d, for an S3 range GET."""
    _, _, sub = _parts(d, subshards)
    start = sub * m // 8
    return start, start + m // 8 - 1


def might_contain(subshard_bits, d, subshards, m):
    """Check digest d against the bytes of its sub-shard (from byte_range)."""
    h1, h2, _ = _parts(d, subshards)
    for i in range(K):
        p = (h1 + i * h2) % m
        if not subshard_bits[p >> 3] >> (p & 7) & 1:
            return False
    return True
