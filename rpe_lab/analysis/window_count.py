#!/usr/bin/env python3
"""Experiment 1: race-window census.

A race window = an eviction of object K by the master while a Get of K is
in flight (t_lookup_ns .. t_done_ns). Each (evict, in-flight read) pair is
the necessary condition for RPE being exercised -- regardless of whether
the race converts to wrong bytes (conversion is measured separately by the
client probe). Counting windows does not require bypassing the guard and
is safe to run for days under natural load.

Inputs (same run):  evict_<run>.jsonl   (master per-key eviction census)
                    req_victim_<run>.jsonl (driver reads)
Output: windows/hour, windows per 1k completed transfers, rc breakdown,
overlap-time distribution. Writes results/windows_<run>.json

Usage: python3 analysis/window_count.py <run_id> [results_dir]
"""

import json
import os
import sys

RC_OK = None  # determined per record


def main():
    run_id = sys.argv[1]
    rdir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results")

    evicts = []
    ev_path = os.path.join(rdir, f"evict_{run_id}.jsonl")
    if os.path.exists(ev_path):
        for line in open(ev_path):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "evict":
                evicts.append((e["ts_ns"], e["key"]))
    evicts.sort()

    reads = []  # (t0, t1, key, rc)
    t_first = t_last = None
    for line in open(os.path.join(rdir, f"req_victim_{run_id}.jsonl")):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "t_lookup_ns" not in r:
            continue
        reads.append((r["t_lookup_ns"], r["t_done_ns"], r["key"], r["rc"]))
        t_first = r["t_lookup_ns"] if t_first is None else min(t_first, r["t_lookup_ns"])
        t_last = r["t_done_ns"] if t_last is None else max(t_last, r["t_done_ns"])

    dur_s = (t_last - t_first) / 1e9 if t_first and t_last else 0

    # per-key eviction timestamps for fast lookup
    ev_by_key = {}
    for ts, key in evicts:
        ev_by_key.setdefault(key, []).append(ts)

    windows = []  # (key, read_rc, evict_ts, t0, t1)
    for t0, t1, key, rc in reads:
        for ts in ev_by_key.get(key, []):
            if t0 <= ts <= t1:
                windows.append((key, rc, ts, t0, t1))

    def rate_per_hour(n):
        return round(n / dur_s * 3600, 1) if dur_s else 0

    rc_names = {}
    for _k, rc, *_ in windows:
        rc_names[rc] = rc_names.get(rc, 0) + 1
    completed = sum(1 for r in reads if r[3] != -704)  # transfers that ran
    overlap_ms = sorted((ts - t0) / 1e6 for _k, _r, ts, t0, _t in windows)
    n = len(overlap_ms)

    out = {
        "run_id": run_id,
        "duration_s": round(dur_s, 1),
        "evictions": len(evicts),
        "reads_total": len(reads),
        "transfers_completed": completed,
        "race_windows": len(windows),
        "race_windows_per_hour": rate_per_hour(len(windows)),
        "race_windows_per_1k_transfers": round(1000 * len(windows) / completed, 2) if completed else 0,
        "windows_by_read_rc": rc_names,
        "evict_after_lookup_ms": {
            "p50": round(overlap_ms[n // 2], 1) if n else None,
            "p90": round(overlap_ms[int(n * .9)], 1) if n else None,
            "p99": round(overlap_ms[int(n * .99)], 1) if n else None,
            "max": round(overlap_ms[-1], 1) if n else None,
        },
        "note": "race window = master eviction of K during an in-flight Get of K; "
                "necessary condition for RPE. Conversion to wrong bytes measured "
                "separately (client probe rpe/torn events).",
    }
    dst = os.path.join(rdir, f"windows_{run_id}.json")
    with open(dst, "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=1))
    print("->", dst)


if __name__ == "__main__":
    main()
