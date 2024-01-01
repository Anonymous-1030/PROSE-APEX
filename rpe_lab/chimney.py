#!/usr/bin/env python3
"""Phase 2 end-to-end chimney (plan section 4.4, second DoD).

Forces a real lease-expiry discard with the patched client:
  put one 3.5MB object -> add tc netem delay on loopback so the data
  transfer outlives the lease (TTL=500ms master, smoke-only value) ->
  get_into must return -707 and the passive probe must write a complete
  discard event to $RPE_LAB_EVENTS.

Also verifies event fields: found_magic, found_key_hash == fnv1a64(key),
found_gen == 1 (no overwrite happened in this chimney -- overwrite
detection itself is unit-tested in test_probe.cpp / test_rpe_header.py).

Usage: RPE_LAB_EVENTS=<path> python3 chimney.py   (master must be running)
Exit 0 on success.
"""

import ctypes
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rpe_header as rh  # noqa: E402

LEASE_EXPIRED = -707
EVENTS = os.environ["RPE_LAB_EVENTS"]


def tc(action, delay_ms=None):
    cmd = ["sudo", "-n", "tc", "qdisc", action, "dev", "lo", "root"]
    if action == "add":
        cmd += ["netem", "delay", f"{delay_ms}ms"]
    subprocess.run(cmd, check=True)


def main():
    from mooncake.store import MooncakeDistributedStore
    s = MooncakeDistributedStore()
    rc = s.setup("localhost", "P2PHANDSHAKE", 2 * 1024**3, 16 * 1024**2,
                 "tcp", "", "127.0.0.1:50051")
    if rc != 0:
        sys.exit(f"setup failed rc={rc}")
    key = "hot/0000"
    size = 3_670_016
    rc = s.put(key, rh.make_payload(1, key, 1, time.time_ns(), size))
    if rc != 0:
        sys.exit(f"put failed rc={rc}")
    print("PUT_OK", flush=True)

    buf = ctypes.create_string_buffer(size)
    got_discard = False
    try:
        for delay in (200, 400, 800):
            tc("add", delay)
            print(f"tc delay {delay}ms on lo", flush=True)
            for attempt in range(3):
                t0 = time.monotonic()
                rc = s.get_into(key, ctypes.addressof(buf), size)
                dt = time.monotonic() - t0
                print(f"  get_into rc={rc} ({dt:.2f}s)", flush=True)
                if rc == LEASE_EXPIRED:
                    got_discard = True
                    break
            tc("del")
            if got_discard:
                break
    finally:
        subprocess.run(["sudo", "-n", "tc", "qdisc", "del", "dev", "lo", "root"],
                       capture_output=True)
    if not got_discard:
        sys.exit("no discard observed at any delay")

    time.sleep(0.5)
    events = [json.loads(l) for l in open(EVENTS) if l.strip()]
    discards = [e for e in events if e.get("type") == "discard"]
    if not discards:
        sys.exit("discard observed via rc but no event written")
    e = discards[-1]
    ok = (e["found_magic"] and e["found_key_hash"] == rh.key_hash(key)
          and e["found_gen"] == 1 and e["payload_len"] == size
          and e["expired_by_us"] > 0)
    print("EVENT:", json.dumps(e), flush=True)
    print("CHIMNEY_PASS" if ok else "CHIMNEY_FIELD_MISMATCH", flush=True)
    s.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
