#!/usr/bin/env python3
"""Low-oversubscription extension of the optimistic-reclaim sweep (P1-2).

Reuses the count-based engine from run_optimistic_reclaim.py but sweeps
2x/4x/8x oversubscription, preserving the same paired-trace mechanics,
10 seeds, 512-entry pool, and 32-object admit budget.

Outputs:
  experiments/out/optimistic_reclaim_low/optimistic_reclaim_low.csv
  experiments/out/optimistic_reclaim_low/optimistic_reclaim_low.json
  experiments/out/optimistic_reclaim_low/optimistic_reclaim_low_summary.txt
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean, pstdev

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import experiments.run_optimistic_reclaim as base

base.OUT = ROOT / "experiments" / "out" / "optimistic_reclaim_low"
base.OVERSUB = [2, 4, 8]
base.TENANTS = [8, 16, 32]
base.SEEDS = list(range(10))
base.BUDGET = 32
base.CAPACITY = 512
base.N_STEPS = 200
base.BOUND_MODES = ["capacity", "token_table"]


def main():
    base.OUT.mkdir(parents=True, exist_ok=True)
    all_rows = []
    for bound_mode in base.BOUND_MODES:
        rows = base.run_grid(bound_mode)
        all_rows.extend(rows)
    agg = base.aggregate(all_rows)

    # Save per-run CSV
    import csv
    csv_path = base.OUT / "optimistic_reclaim_low.csv"
    keys = ["mechanism", "label", "oversubscription", "n_tenants", "bound_mode",
            "bound", "seed", "valid_throughput_Bpns", "admission_p99_ns",
            "pinned_peak", "reclaimable_capacity_frac", "rpe_payload_frac"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in all_rows:
            writer.writerow({k: r.get(k, "") for k in keys})

    # Save aggregated JSON keyed like the original script
    json_path = base.OUT / "optimistic_reclaim_low.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump({"|".join(str(x) for x in k): v for k, v in agg.items()}, f, indent=2)

    # Human-readable summary for 16 tenants, capacity mode
    lines = []
    lines.append("Low-oversubscription optimistic-reclaim sweep (16 tenants, capacity mode)")
    lines.append(f"{'Oversub':>8} {'Mech':>8} {'ValidThr':>10} {'P99 admit':>12} "
                 f"{'Peak pin':>9} {'Recl frac':>10} {'RPE payload':>12}")
    for oversub in base.OVERSUB:
        for mech in base.MECH_ORDER:
            k = ("capacity", mech, oversub, 16)
            if k not in agg:
                continue
            v = agg[k]
            lines.append(
                f"{oversub:>8} {mech:>8} {v['valid_throughput_Bpns']:>10.3f} "
                f"{v['admission_p99_ns']:>12.1f} {int(v['pinned_peak']):>9} "
                f"{v['reclaimable_capacity_frac']*100:>9.1f}% "
                f"{v['rpe_payload_frac']*100:>11.1f}%"
            )
    txt = "\n".join(lines)
    (base.OUT / "optimistic_reclaim_low_summary.txt").write_text(txt + "\n")
    print(txt)
    print(f"\nWrote {csv_path}, {json_path}, and summary.")


if __name__ == "__main__":
    main()
