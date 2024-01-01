#!/usr/bin/env python3
"""Experiment 3 support: how much of BurstGPT time/requests lives in bursts.

Computes, over the full 61-day BurstGPT_1 trace:
  - per-second arrival-rate distribution
  - share of TIME with rate > k * trace mean (k = 4, 16)
  - share of REQUESTS falling in those seconds
  - the densest 10k-request window (used by our drivers) and its stats

These numbers let the paper phrase the constructed congestion window as a
trace-native percentile condition rather than an adversarial injection.

Usage: python3 analysis/trace_stats.py [trace_csv]
Output: results/trace_stats.json
"""

import collections
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TRACE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    HERE, "..", "trace", "BurstGPT_1.csv")

ts = []
with open(TRACE, newline="") as f:
    rd = csv.reader(f)
    next(rd)
    for row in rd:
        try:
            ts.append(float(row[0]))
        except (ValueError, IndexError):
            continue
ts.sort()

span = ts[-1] - ts[0]
mean_rate = len(ts) / span
per_sec = collections.Counter(int(t) for t in ts)
rates = sorted(per_sec.values())
n = len(rates)

out = {
    "trace": os.path.basename(TRACE),
    "requests": len(ts),
    "span_days": round(span / 86400, 1),
    "mean_rate_per_s": round(mean_rate, 3),
    "per_second_rate": {
        "p50": rates[n // 2],
        "p90": rates[int(n * .9)],
        "p99": rates[int(n * .99)],
        "max": rates[-1],
    },
    "burst_share": {},
}
for k in (4, 16):
    thr = k * mean_rate
    secs = sum(1 for r in per_sec.values() if r > thr)
    reqs = sum(r for r in per_sec.values() if r > thr)
    out["burst_share"][f"gt_{k}x_mean"] = {
        "threshold_per_s": round(thr, 2),
        "time_share_pct": round(100 * secs / n, 2),
        "request_share_pct": round(100 * reqs / len(ts), 2),
    }

# densest 10k-request window (what our drivers replay)
W = 10000
best = None
for i in range(0, len(ts) - W, 2000):
    s = ts[i + W] - ts[i]
    if best is None or s < best[0]:
        best = (s, i)
out["densest_10k_window"] = {
    "start_row": best[1], "span_s": round(best[0], 1),
    "mean_rate_per_s": round(W / best[0], 1),
    "vs_trace_mean": round((W / best[0]) / mean_rate, 1),
}

dst = os.path.join(HERE, "..", "results", "trace_stats.json")
with open(dst, "w") as f:
    json.dump(out, f, indent=2)
print(json.dumps(out, indent=1))
print("->", dst)
