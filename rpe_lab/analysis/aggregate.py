#!/usr/bin/env python3
"""Aggregate rpe_lab results into the paper's macro table.

Reads results/tier*.json (+ req_victim_*.jsonl / events_*.jsonl for
burst_share) and prints a per-run table plus the nine paper macros.

Usage: python3 analysis/aggregate.py [results_dir]
"""

import glob
import json
import os
import sys

MACROS = [
    ("\\McDiscards", "guard_fires"),
    ("\\McTierAFires", "guard_fires_tierA"),
    ("\\McRaces", "rpe_events"),
    ("\\McRPEBytes", "rpe_payload_bytes"),
    ("\\McRPERate", "rpe_payload_rate_pct"),
    ("\\McMisBW", "misbw_bytes"),
    ("\\McBurstShare", "burst_share_pct"),
    ("\\McPinThr", "throughput_ratio_pin"),
    ("\\McPinRecl", "reclaimable_pct"),
]

BURST_BIN_S = 10.0
BURST_MULT = 4.0


def burst_share(results_dir, run_id):
    """Share of discard events landing in burst bins (arrival rate > 4x mean)."""
    req = os.path.join(results_dir, f"req_victim_{run_id}.jsonl")
    ev = os.path.join(results_dir, f"events_{run_id}.jsonl")
    if not (os.path.exists(req) and os.path.exists(ev)):
        return None
    arrivals = []
    for line in open(req):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "t_lookup_ns" in r:
            arrivals.append(r["t_lookup_ns"])
    events = []
    for line in open(ev):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "discard":
            events.append(e["ts_ns"])
    if not arrivals or not events:
        return 0.0 if events else None
    t0 = min(arrivals)
    bins = {}
    for t in arrivals:
        bins[int((t - t0) / (BURST_BIN_S * 1e9))] = bins.get(int((t - t0) / (BURST_BIN_S * 1e9)), 0) + 1
    mean_rate = sum(bins.values()) / max(len(bins), 1)
    burst_bins = {b for b, n in bins.items() if n > BURST_MULT * mean_rate}
    in_burst = sum(1 for t in events if int((t - t0) / (BURST_BIN_S * 1e9)) in burst_bins)
    return round(100.0 * in_burst / len(events), 2)


def main():
    rdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "results")
    rows = []
    for p in sorted(glob.glob(os.path.join(rdir, "tier*.json"))):
        with open(p) as f:
            r = json.load(f)
        r["_file"] = os.path.basename(p)
        if r.get("burst_share_pct") is None:
            r["burst_share_pct"] = burst_share(rdir, r["run_id"])
        rows.append(r)
    if not rows:
        sys.exit(f"no tier*.json in {rdir}")

    cols = ["run_id", "tier", "ttl_ms", "concurrency", "seed", "duration_s",
            "gets_total", "gets_ok", "guard_fires", "rpe_events",
            "rpe_payload_bytes", "torn_events", "gen_skew_events",
            "rpe_payload_rate_pct", "misbw_bytes",
            "success_mismatch", "no_magic_discards", "not_found",
            "burst_share_pct", "throughput_mbps", "put_failures_pool_full"]
    print("\t".join(cols))
    for r in rows:
        mf = r.get("master_flags", {})
        print("\t".join(str(x) for x in [
            r["run_id"], r.get("tier"), mf.get("default_kv_lease_ttl"),
            r.get("concurrency", "-"), r.get("seed", "-"), r.get("duration_s"),
            r.get("gets_total"), r.get("gets_ok"), r.get("guard_fires"),
            r.get("rpe_events"), r.get("rpe_payload_bytes"),
            r.get("torn_events"), r.get("gen_skew_events"),
            r.get("rpe_payload_rate_pct"), r.get("misbw_bytes"),
            r.get("success_mismatch"), r.get("no_magic_discards"),
            r.get("not_found"), r.get("burst_share_pct"),
            r.get("throughput_mbps"), r.get("put_failures_pool_full")]))

    tier_a = [r for r in rows if r.get("tier") == "A"]
    print("\n== paper macros ==")
    print("\\McDiscards (guard_fires, all):",
          sum(r.get("guard_fires", 0) for r in rows))
    print("\\McTierAFires (guard_fires, tier A):",
          sum(r.get("guard_fires", 0) for r in tier_a))
    print("\\McRaces (rpe_events):",
          sum(r.get("rpe_events", 0) for r in rows))
    print("\\McRPEBytes:", sum(r.get("rpe_payload_bytes", 0) for r in rows))
    tot_pay = sum(r.get("payload_bytes_total", 0) for r in rows)
    print("\\McRPERate (%):",
          round(100.0 * sum(r.get("rpe_payload_bytes", 0) for r in rows) / tot_pay, 6) if tot_pay else 0)
    print("\\McMisBW:", sum(r.get("misbw_bytes", 0) for r in rows))
    bs = [r["burst_share_pct"] for r in rows if r.get("burst_share_pct") is not None]
    print("\\McBurstShare (%):", round(sum(bs) / len(bs), 2) if bs else "n/a")
    print("\\McPinThr / \\McPinRecl: see Phase 5 (expB_*.json)")
    sm = sum(r.get("success_mismatch", 0) for r in rows)
    print(f"success_mismatch total: {sm} {'(must be 0)' if sm == 0 else '*** RED LINE ***'}")
    # Experiment 2 framing: absolute wire-level wrong-byte rates per run
    print("\n== wire-level wrong bytes (absolute rates) ==")
    for r in rows:
        wb = r.get("rpe_payload_bytes", 0) + r.get("torn_payload_bytes", 0) or 0
        dur = r.get("duration_s") or 0
        if dur:
            print(f"  {r['run_id']}: {wb / 1e6 / dur * 3600:.2f} MB/h on wire "
                  f"({r.get('rpe_events', 0)}+{r.get('torn_events', 0)} events)")


if __name__ == "__main__":
    main()
