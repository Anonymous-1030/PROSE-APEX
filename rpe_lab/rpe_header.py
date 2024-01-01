"""RPE-lab payload identity header (see plan section 4.1).

Every object written by the driver carries a 64-byte identity header;
the remaining bytes are pseudo-random filler. The checker (both the C++
passive probe before the discard point and the driver-side validator)
parses this header and compares it against the expected
(key_hash, generation) pair.

Layout (all integers little-endian):

    offset  field
    0..7    magic = b"RPELAB_1" (0x5250454C41425F31 read as ASCII)
    8..15   tenant_id (uint64)
    16..23  key_hash  (uint64, FNV-1a-64 of the key string)
    24..31  generation (uint64, n-th Put of this key)
    32..39  put_timestamp_ns (uint64)
    40..63  reserved = 0
"""

import os
import struct

MAGIC = b"RPELAB_1"  # 0x52 0x50 0x45 0x4C 0x41 0x42 0x5F 0x31
HEADER_SIZE = 64
_STRUCT = struct.Struct("<8s4Q24s")  # magic, tenant, key_hash, gen, ts_ns, reserved

FNV1A64_OFFSET = 14695981039346656037
FNV1A64_PRIME = 1099511628211
_MASK64 = (1 << 64) - 1

TENANT_A = 1  # victim: hot-set objects, Get workload
TENANT_B = 2  # pressure source: Put workload, overwrites reclaimed slots


def fnv1a64(data: bytes) -> int:
    h = FNV1A64_OFFSET
    for b in data:
        h ^= b
        h = (h * FNV1A64_PRIME) & _MASK64
    return h


def key_hash(key: str) -> int:
    return fnv1a64(key.encode("utf-8"))


def make_header(tenant_id: int, key: str, generation: int, ts_ns: int) -> bytes:
    return _STRUCT.pack(MAGIC, tenant_id, key_hash(key), generation, ts_ns, b"")


def make_payload(tenant_id: int, key: str, generation: int, ts_ns: int,
                 size: int) -> bytes:
    """Header (first 64B) + pseudo-random filler + header copy (last 64B).

    The tail marker detects torn reads: if the slot is overwritten while a
    transfer is in flight, the head may have been pulled before the overwrite
    while the tail is pulled after -> head/tail identity mismatch.
    """
    if size < 2 * HEADER_SIZE:
        raise ValueError(f"payload size {size} < 2x header size")
    hdr = make_header(tenant_id, key, generation, ts_ns)
    return hdr + os.urandom(size - 2 * HEADER_SIZE) + hdr


def _parse_at(buf, off) -> dict:
    out = {"magic": False, "tenant": None, "key_hash": None, "gen": None}
    if buf is None or len(buf) < off + HEADER_SIZE:
        return out
    magic, tenant, khash, gen, _ts, _res = _STRUCT.unpack(
        bytes(buf[off:off + HEADER_SIZE]))
    if magic != MAGIC:
        return out
    out.update(magic=True, tenant=tenant, key_hash=khash, gen=gen)
    return out


def parse_header(buf) -> dict:
    """Parse head (offset 0) and tail (last 64B) identity markers."""
    head = _parse_at(buf, 0)
    tail = _parse_at(buf, len(buf) - HEADER_SIZE) if buf is not None else \
        {"magic": False, "tenant": None, "key_hash": None, "gen": None}
    return {
        "found_magic": head["magic"],
        "found_tenant": head["tenant"],
        "found_key_hash": head["key_hash"],
        "found_gen": head["gen"],
        "tail_magic": tail["magic"],
        "tail_tenant": tail["tenant"],
        "tail_key_hash": tail["key_hash"],
        "tail_gen": tail["gen"],
    }


def check_payload(buf, expected_key: str, expected_gen: int) -> dict:
    """Classify a received buffer against the expected identity.

    match:        head AND tail both match expectation
    overwritten:  head identity wrong (slot reclaimed before/early in read)
    torn:         head matches but tail differs (overwrite landed mid-read)
    no_magic:     no valid marker, or buffer too short to hold both markers
    """
    info = parse_header(buf)
    exp_hash = key_hash(expected_key)
    info["expected_key_hash"] = exp_hash
    info["expected_gen"] = expected_gen
    if buf is None or len(buf) < 2 * HEADER_SIZE:
        info.update(match=False, overwritten=False, torn=False, no_magic=True)
        return info
    head_ok = (info["found_magic"] and info["found_key_hash"] == exp_hash
               and info["found_gen"] == expected_gen)
    tail_ok = (info["tail_magic"] and info["tail_key_hash"] == exp_hash
               and info["tail_gen"] == expected_gen)
    info["match"] = head_ok and tail_ok
    info["overwritten"] = info["found_magic"] and not head_ok
    info["torn"] = head_ok and not tail_ok
    info["no_magic"] = not info["found_magic"] and not info["tail_magic"]
    return info
