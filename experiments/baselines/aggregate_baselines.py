#!/usr/bin/env python3
"""Aggregate the per-run baseline CSV into the paper-level summary.

Statistical rules (spec §XIII):
  1. Throughput is normalized to NoCheck WITHIN each (workload, seed) pair.
  2. Cross-(workload,seed) reduction uses the GEOMETRIC MEAN of those paired
     normalized ratios (never a raw GB/s average).
  3. Confidence intervals use a PAIRED bootstrap: resample (workload, seed)
     pairs with replacement, recompute the geometric mean each resample.
  4. Stale is a GLOBAL byte-weighted ratio sum(stale)/sum(requested), not a mean
     of per-run ratios. CI via the same paired bootstrap over the byte totals.
  5. Control/header overhead is likewise a global byte ratio.
  6. The figure and this CSV are produced from THIS ONE dataframe. Nothing is
     hand-filled downstream.

Output: results/baselines/summary_aggregate.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.baselines.baseline_common import METHOD_ORDER

RESULTS = ROOT / "results" / "baselines"
IN_CSV = RESULTS / "summary_by_run.csv"
OUT_CSV = RESULTS / "summary_aggregate.csv"
N_BOOTSTRAP = 10000
BOOTSTRAP_SEED = 20260714

AGG_COLUMNS = [
    "method", "segment_bytes",
    "normalized_throughput_gmean",
    "normalized_throughput_ci_low", "normalized_throughput_ci_high",
    "stale_mib_per_gib", "stale_ci_low", "stale_ci_high",
    "extra_rtt",
    "pin_span_ratio_median", "pin_span_ratio_p95",
    "control_header_overhead_pct",
    "queue_reclaim", "rpe_events", "num_runs",
]


def load_runs(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def geomean(xs: np.ndarray) -> float:
    xs = np.asarray(xs, dtype=float)
    xs = np.clip(xs, 1e-12, None)
    return float(np.exp(np.mean(np.log(xs))))


def aggregate(rows: List[Dict[str, str]]) -> List[Dict[str, object]]:
    # index rows by method, and by (workload, seed) for the NoCheck baseline.
    by_method: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    nocheck_tp: Dict[Tuple[str, str], float] = {}
    for r in rows:
        by_method[r["method"]].append(r)
        if r["method"] == "NoCheck":
            nocheck_tp[(r["workload"], r["seed"])] = float(r["valid_throughput_gbps"])

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    out: List[Dict[str, object]] = []

    for method in METHOD_ORDER:
        mrows = by_method[method]
        if not mrows:
            continue            # method absent from this input CSV
        # align each run to its paired NoCheck throughput
        pairs = []
        stale_num, stale_den = [], []
        ch_num, ch_den = [], []
        for r in mrows:
            key = (r["workload"], r["seed"])
            base = nocheck_tp[key]
            norm = float(r["valid_throughput_gbps"]) / base if base > 0 else 0.0
            pairs.append(norm)
            stale_num.append(float(r["total_stale_bytes"]))
            stale_den.append(float(r["total_requested_bytes"]))
            ch_num.append(float(r["total_control_bytes"]) + float(r["total_header_bytes"]))
            ch_den.append(float(r["total_wire_bytes"]))
        pairs = np.array(pairs)
        stale_num = np.array(stale_num); stale_den = np.array(stale_den)
        ch_num = np.array(ch_num); ch_den = np.array(ch_den)
        n = len(pairs)

        gm = geomean(pairs)
        stale_ratio = stale_num.sum() / max(1.0, stale_den.sum()) * 1024.0
        ch_pct = ch_num.sum() / max(1.0, ch_den.sum()) * 100.0

        # paired bootstrap over run indices
        idx = rng.integers(0, n, size=(N_BOOTSTRAP, n))
        gm_bs = np.exp(np.mean(np.log(np.clip(pairs[idx], 1e-12, None)), axis=1))
        stale_bs = (stale_num[idx].sum(axis=1)
                    / np.clip(stale_den[idx].sum(axis=1), 1.0, None)) * 1024.0
        gm_lo, gm_hi = np.percentile(gm_bs, [2.5, 97.5])
        st_lo, st_hi = np.percentile(stale_bs, [2.5, 97.5])

        # pin span: median-of-run-medians, p95-of-run-p95 (over runs with a pin)
        pin_med = float(np.median([float(r["pin_span_ratio_median"]) for r in mrows]))
        pin_p95 = float(np.percentile(
            [float(r["pin_span_ratio_p95"]) for r in mrows], 95))
        rpe = int(sum(int(r["rpe_events"]) for r in mrows))
        extra_rtt = int(float(mrows[0]["extra_rtt"]))
        qr = mrows[0]["queue_reclaim"]
        seg = int(float(mrows[0]["segment_bytes"]))

        out.append({
            "method": method,
            "segment_bytes": seg,
            "normalized_throughput_gmean": gm,
            "normalized_throughput_ci_low": float(gm_lo),
            "normalized_throughput_ci_high": float(gm_hi),
            "stale_mib_per_gib": stale_ratio,
            "stale_ci_low": float(st_lo),
            "stale_ci_high": float(st_hi),
            "extra_rtt": extra_rtt,
            "pin_span_ratio_median": pin_med,
            "pin_span_ratio_p95": pin_p95,
            "control_header_overhead_pct": ch_pct,
            "queue_reclaim": qr,
            "rpe_events": rpe,
            "num_runs": n,
        })
    return out


def write_csv(rows: List[Dict[str, object]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=AGG_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in-csv", type=Path, default=IN_CSV)
    ap.add_argument("--out-csv", type=Path, default=OUT_CSV)
    args = ap.parse_args()

    rows = load_runs(args.in_csv)
    agg = aggregate(rows)
    write_csv(agg, args.out_csv)

    print(f"Aggregated {len(rows)} runs -> {args.out_csv}")
    print(f"{'method':16s} {'norm_tp':>8s} {'stale':>10s} {'ovh%':>7s} "
          f"{'+RTT':>4s} {'pin':>6s} {'Qrec':>4s} {'rpe':>5s}")
    for r in agg:
        print(f"{r['method']:16s} {r['normalized_throughput_gmean']:8.3f} "
              f"{r['stale_mib_per_gib']:10.3f} "
              f"{r['control_header_overhead_pct']:7.2f} "
              f"{r['extra_rtt']:>4d} {r['pin_span_ratio_median']:6.2f} "
              f"{r['queue_reclaim']:>4s} {r['rpe_events']:>5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
