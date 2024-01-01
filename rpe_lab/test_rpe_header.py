"""Unit test for the RPE-lab payload header checker v2 (head+tail markers).

Run: python test_rpe_header.py
"""

import os

import rpe_header as rh


def case(name: str, ok: bool):
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        raise SystemExit(f"case failed: {name}")


SIZE = 3_670_016

# 1. correct payload: head and tail match
buf = rh.make_payload(rh.TENANT_A, "hot/key/017", 3, 1_755_000_000_000_000_000, SIZE)
r = rh.check_payload(buf, "hot/key/017", 3)
case("correct payload matches", r["match"] and not r["overwritten"] and not r["torn"])
case("payload size exact", len(buf) == SIZE)
case("tail marker present", buf[-8:] == buf[56:64] and buf[:8] == rh.MAGIC and buf[-64:-56] == rh.MAGIC)

# 2. foreign generation (slot rewritten): head wrong
buf2 = rh.make_payload(rh.TENANT_A, "hot/key/017", 7, 1_755_000_001_000_000_000, SIZE)
r2 = rh.check_payload(buf2, "hot/key/017", 3)
case("wrong gen -> overwritten", r2["overwritten"] and r2["found_gen"] == 7 and not r2["match"])

# 2b. cross-tenant overwrite
buf2b = rh.make_payload(rh.TENANT_B, "bulk/obj/991", 1, 1_755_000_002_000_000_000, SIZE)
r2b = rh.check_payload(buf2b, "hot/key/017", 3)
case("foreign object -> overwritten",
     r2b["overwritten"] and r2b["found_tenant"] == rh.TENANT_B)

# 3. torn read: head from old object, tail from new object
torn = bytearray(buf)          # correct gen=3
torn[-64:] = rh.make_header(rh.TENANT_B, "bulk/obj/991", 1, 0)  # tail overwritten
r3 = rh.check_payload(bytes(torn), "hot/key/017", 3)
case("torn read detected", r3["torn"] and not r3["match"] and not r3["overwritten"]
     and r3["tail_tenant"] == rh.TENANT_B)

# 4. no magic anywhere
r4 = rh.check_payload(os.urandom(SIZE), "hot/key/017", 3)
case("no magic counted separately", r4["no_magic"] and not r4["overwritten"] and not r4["torn"])

# 4b. zeroed buffer
case("zeroed buffer -> no_magic", rh.check_payload(bytes(SIZE), "hot/key/017", 3)["no_magic"])

# 5. truncated buffer
case("short buffer -> no_magic, no crash", rh.check_payload(buf[:100], "hot/key/017", 3)["no_magic"])

# 6. FNV-1a-64 known vectors
case("fnv1a64 empty == offset basis", rh.fnv1a64(b"") == 14695981039346656037)
case("fnv1a64 'a' == 0xAF63DC4C8601EC8C", rh.fnv1a64(b"a") == 0xAF63DC4C8601EC8C)

print("all rpe_header v2 unit tests passed")
