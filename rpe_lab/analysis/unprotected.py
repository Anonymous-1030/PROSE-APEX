#!/usr/bin/env python3
"""Four-quadrant comparison: guard-on vs guard-off (unprotected baseline).

Quadrants per regime (same seed, same workload):
  wire-arrived, guard-on   = rpe_payload_bytes (C++ discard events)
  delivered,    guard-on   = 0 by construction (detect-and-discard)
  wire-arrived, guard-off  = rpe_payload_bytes (same instrumentation)
  delivered,    guard-off  = delivered_wrong_bytes (checker sees what the
                             consumer WOULD have received; count-and-quarantine)

Rates are normalized by delivered-good bytes so they line up with the
SimCXL MisBW / RPE_payload accounting.

Usage: python3 analysis/unprotected.py <on_run_id> <off_run_id> [results_dir]
"""

import json
import os
import sys

OBJ = 3670016


def load(rdir, run_id):
    for tier in ("tierA", "tierB", "tierU"):
        p = os.path.join(rdir, f"{tier}_{run_id}.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    sys.exit(f"no results for {run_id} in {rdir}")


def main():
    on_id, off_id = sys.argv[1], sys.argv[2]
    rdir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results")
    on, off = load(rdir, on_id), load(rdir, off_id)

    wire_on = on.get("rpe_payload_bytes", 0)
    ok_on = on.get("payload_bytes_total", 0)
    misbw_on = on.get("misbw_bytes", 0)

    wire_off = off.get("rpe_payload_bytes", 0)
    ok_off = off.get("payload_bytes_total", 0)
    dwrong_off = off.get("delivered_wrong_bytes", 0)
    dwrong_ev_off = off.get("delivered_wrong_events", 0)

    def pct(a, b):
        return round(100.0 * a / b, 6) if b else 0.0

    out = {
        "regime": {"on": on_id, "off": off_id},
        "commit": on.get("commit"),
        "guard_on": {
            "guard_fires": on.get("guard_fires", 0),
            "wire_wrong_bytes": wire_on,
            "delivered_wrong_bytes": 0,
            "delivered_good_bytes": ok_on,
            "misbw_bytes": misbw_on,
            "wire_wrong_per_wire_byte_pct": pct(wire_on, ok_on + misbw_on),
            "exposure_per_delivered_byte_pct": 0.0,
        },
        "guard_off": {
            "guard_fires": off.get("guard_fires", 0),
            "wire_wrong_bytes": wire_off,
            "delivered_wrong_bytes": dwrong_off,
            "delivered_wrong_events": dwrong_ev_off,
            "delivered_good_bytes": ok_off,
            "wire_wrong_per_wire_byte_pct": pct(wire_off, ok_off),
            "unprotected_wrong_per_delivered_pct": pct(dwrong_off, ok_off),
        },
        "interpretation": {
            "guard_blocked_wrong_bytes": wire_off,
            "guard_block_rate_pct": 100.0,
            "note": "guard-on delivers zero wrong bytes by construction "
                    "(detect-and-discard); the baseline shows what the "
                    "consumer would have received unprotected.",
        },
    }
    dst = os.path.join(rdir, f"unprotected_{off_id}.json")
    with open(dst, "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=1))
    print("->", dst)


if __name__ == "__main__":
    main()
