#!/usr/bin/env python3
"""Aggregate Tier-B probe runs (constructed delay between Query and Get).

The rpe_probe binary writes per-iteration JSONL records:
  {"ts_ns","key","rc","t_query_ns","t_get_done_ns","delay_ms",
   "found_magic","found_tenant","found_key_hash","found_gen",
   "tail_magic","tail_tenant","tail_key_hash","tail_gen"}

Expected generations are reconstructed from the victim request log's
reseed records (gen(t) = 1 + #reseeds of the key before t), so the join
does not depend on the driver's per-request bookkeeping.

Usage: python3 analysis/tierb_aggregate.py <run_id> [results_dir]
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rpe_header as rh  # noqa: E402

LEASE_EXPIRED = -707
OBJECT_NOT_FOUND = -704


def gen_timeline(victim_log):
    """key -> sorted list of (ts_ns, gen) reseed points."""
    tl = {}
    if not os.path.exists(victim_log):
        return tl
    for line in open(victim_log):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") == "reseed" and r.get("rc") == 0:
            tl.setdefault(r["key"], []).append((r["ts_ns"], r["gen"]))
    for v in tl.values():
        v.sort()
    return tl


def gen_at(tl, key, ts_ns):
    g = 1
    for ts, gen in tl.get(key, []):
        if ts <= ts_ns:
            g = gen
        else:
            break
    return g


def classify(found_ok, identity_ok):
    pass  # placeholder for clarity below


def main():
    run_id = sys.argv[1]
    rdir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results")
    tl = gen_timeline(os.path.join(rdir, f"req_victim_{run_id}.jsonl"))

    stats = {"run_id": run_id, "tier": "B", "constructed": True,
             "iterations": 0, "gets_ok": 0, "guard_fires": 0, "not_found": 0,
             "other_err": 0, "rpe_events": 0, "rpe_payload_bytes": 0,
             "torn_events": 0, "torn_payload_bytes": 0,
             "gen_skew_events": 0,
             "no_magic_discards": 0, "success_mismatch": 0,
             "mismatch_detail": [], "payload_bytes_total": 0}
    for path in sorted(glob.glob(os.path.join(rdir, f"probe_{run_id}_*.jsonl"))):
        for line in open(path):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            stats["iterations"] += 1
            rc = e.get("rc")
            key = e.get("key", "")
            exp_gen = gen_at(tl, key, e.get("t_query_ns", e.get("ts_ns", 0)))
            kh = rh.key_hash(key)
            plen = e.get("payload_len", 0)

            def coherent_ok():
                # success requires: both markers present, both for THIS key,
                # and coherent with each other (no torn read)
                return (e.get("found_magic") and e.get("tail_magic")
                        and e.get("found_key_hash") == kh
                        and e.get("tail_key_hash") == kh
                        and e.get("found_gen") == e.get("tail_gen"))

            if rc == 0:
                stats["gets_ok"] += 1
                stats["payload_bytes_total"] += plen
                if not coherent_ok():
                    stats["success_mismatch"] += 1
                    stats["mismatch_detail"].append(e)
            elif rc == LEASE_EXPIRED:
                stats["guard_fires"] += 1
                head_foreign = e.get("found_magic") and e.get("found_key_hash") != kh
                tail_foreign = e.get("tail_magic") and e.get("tail_key_hash") != kh
                if not e.get("found_magic") and not e.get("tail_magic"):
                    stats["no_magic_discards"] += 1
                elif head_foreign or tail_foreign:
                    # unambiguous RPE: another object's bytes (foreign key)
                    stats["rpe_events"] += 1
                    stats["rpe_payload_bytes"] += plen
                    if not head_foreign and tail_foreign:
                        stats["torn_events"] += 1
                        stats["torn_payload_bytes"] += plen
                elif (e.get("found_magic") and e.get("found_gen") != exp_gen) or \
                        (e.get("tail_magic") and e.get("tail_gen") != exp_gen):
                    # ambiguous same-key gen skew (see NOTES); not RPE
                    stats["gen_skew_events"] += 1
            elif rc == OBJECT_NOT_FOUND:
                stats["not_found"] += 1
            else:
                stats["other_err"] += 1

    out = os.path.join(rdir, f"tierB_{run_id}.json")
    with open(out, "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps({k: v for k, v in stats.items() if k != "mismatch_detail"},
                     indent=1))
    print("->", out)
    if stats["success_mismatch"]:
        print("*** RED LINE: success_mismatch > 0 ***")


if __name__ == "__main__":
    main()
