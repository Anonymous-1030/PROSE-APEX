#!/usr/bin/env python3
"""Experiment A — Optimistic Reclaim under Extreme Oversubscription (§IV-C/§IV-D).

Shows that TRANSFER-SCOPED protection (PROSE) unlocks a capability EARLY
protection (REFCNT / 2PHASE) cannot: under extreme oversubscription it keeps
reclamation legal until a transfer is actually admitted, so pinned state stays
tiny, reclaimable capacity stays high, and P99 admission latency stays flat —
while early protection pins the whole oversubscribed candidate backlog, exhausts
the bounded pin/reservation pool, and its admission tail explodes.

Paired comparison: every mechanism replays the IDENTICAL offered-load trajectory
(build steps, candidate objects, cold-miss flags, mid-transfer reuse attempts)
generated once per (oversub, tenants, seed). Only the protection window differs.

Cross-validation of the back-pressure source (--bound-mode / both):
  * capacity    — pinned objects occupy endpoint slots and make them
                  non-reclaimable (capacity-reclaim model).
  * token_table — pinned objects occupy a bounded reservation/pin token table
                  (Little's-law token-saturation model).
Both produce the same qualitative conclusion, so it is not an artifact of one
modeling choice.

Outputs (experiments/out/optimistic_reclaim/):
  * fig_optimistic_reclaim.{pdf,png}  — 3 subplots (throughput, P99, pinned/recl)
  * optimistic_reclaim.csv            — full per-(mech,oversub,tenants,seed,bound) grid
  * optimistic_reclaim_summary.txt    — 32x / 64x summary table
  * optimistic_reclaim.json           — aggregated means for the paper
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.oversub_reclaim import (           # noqa: E402
    OversubConfig, generate_oversub_trace, replay_oversub, MECHS, MECH_ORDER,
)

OUT = ROOT / "experiments" / "out" / "optimistic_reclaim"

# ── Sweep (focused version) ──────────────────────────────────────────────────
OVERSUB = [8, 16, 32, 64]
TENANTS = [8, 16, 32]
SEEDS = list(range(10))
BUDGET = 32
CAPACITY = 512            # bounded pin/slot pool; knee sits inside the sweep
N_STEPS = 200
BOUND_MODES = ["capacity", "token_table"]

MECH_COLORS = {
    "PROSE": "#1b9e77", "REFCNT": "#d95f02", "2PHASE": "#7570b3",
    "GENONLY": "#999999",
}


def run_grid(bound_mode: str) -> List[Dict]:
    rows: List[Dict] = []
    for oversub in OVERSUB:
        for tenants in TENANTS:
            for seed in SEEDS:
                cfg = OversubConfig(
                    oversubscription=oversub, n_tenants=tenants,
                    admit_budget=BUDGET, n_steps=N_STEPS, capacity=CAPACITY,
                    token_table=CAPACITY, bound_mode=bound_mode, seed=seed,
                )
                trace = generate_oversub_trace(cfg)
                for m in MECH_ORDER:
                    r = replay_oversub(trace, MECHS[m])
                    rows.append(r)
    return rows


def aggregate(rows: List[Dict]) -> Dict:
    """Mean over seeds, keyed by (bound_mode, mechanism, oversub, tenants)."""
    agg: Dict = {}
    keys = ("valid_throughput_Bpns", "admission_p99_ns", "pinned_peak",
            "reclaimable_capacity_frac", "nonreclaimable_frac",
            "rpe_payload_frac", "pinned_byte_time_Bns", "backlog_wait_p99_ns")
    buckets: Dict = {}
    for r in rows:
        k = (r["bound_mode"], r["mechanism"], r["oversubscription"], r["n_tenants"])
        buckets.setdefault(k, []).append(r)
    for k, rs in buckets.items():
        agg[k] = {kk: float(mean(r[kk] for r in rs)) for kk in keys}
        agg[k]["throughput_std"] = float(pstdev([r["valid_throughput_Bpns"] for r in rs])) if len(rs) > 1 else 0.0
    return agg


def _norm_throughput(agg: Dict, bound: str, tenants: int) -> Dict:
    """Normalize valid throughput to the UNSAFE-equivalent baseline.

    We normalize each mechanism's valid throughput to PROSE's at the SAME point
    so the figure reads as a relative comparison; PROSE == 1.0 by construction,
    and GENONLY < 1.0 exposes its stale-byte waste. (UNSAFE is off-chart: it
    streams stale bytes as if useful.)"""
    out: Dict = {}
    for oversub in OVERSUB:
        base = agg[(bound, "PROSE", oversub, tenants)]["valid_throughput_Bpns"]
        for m in MECH_ORDER:
            v = agg[(bound, m, oversub, tenants)]["valid_throughput_Bpns"]
            out[(m, oversub)] = v / base if base > 0 else 0.0
    return out


def make_figure(agg: Dict, bound_mode: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tenants = 16   # headline tenant count for the figure
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2))

    # (a) oversubscription vs valid throughput (normalized to PROSE)
    ax = axes[0]
    norm = _norm_throughput(agg, bound_mode, tenants)
    for m in MECH_ORDER:
        ys = [norm[(m, o)] for o in OVERSUB]
        ax.plot(OVERSUB, ys, "o-", label=MECHS[m].label.split(" (")[0],
                color=MECH_COLORS[m], lw=2, ms=6)
    ax.axhline(1.0, color="k", ls=":", lw=0.8, alpha=0.5)
    ax.set_xscale("log", base=2); ax.set_xticks(OVERSUB)
    ax.get_xaxis().set_major_formatter(plt.matplotlib.ticker.ScalarFormatter())
    ax.set_xlabel("Oversubscription (x)")
    ax.set_ylabel("Valid throughput (norm. to PROSE)")
    ax.set_title("(a) Sustained valid throughput")
    ax.legend(fontsize=8, loc="lower left"); ax.grid(alpha=0.3)

    # (b) oversubscription vs P99 admission latency (log y)
    ax = axes[1]
    for m in MECH_ORDER:
        ys = [agg[(bound_mode, m, o, tenants)]["admission_p99_ns"] / 1000.0 for o in OVERSUB]
        ax.plot(OVERSUB, ys, "o-", label=MECHS[m].label.split(" (")[0],
                color=MECH_COLORS[m], lw=2, ms=6)
    ax.set_xscale("log", base=2); ax.set_xticks(OVERSUB)
    ax.get_xaxis().set_major_formatter(plt.matplotlib.ticker.ScalarFormatter())
    ax.set_yscale("log")
    ax.set_xlabel("Oversubscription (x)")
    ax.set_ylabel("P99 admission latency (us)")
    ax.set_title("(b) P99 admission latency")
    ax.legend(fontsize=8, loc="upper left"); ax.grid(alpha=0.3, which="both")

    # (c) oversubscription vs pinned-state / reclaimable capacity
    ax = axes[2]
    for m in ["PROSE", "REFCNT", "2PHASE"]:
        ys = [agg[(bound_mode, m, o, tenants)]["reclaimable_capacity_frac"] * 100 for o in OVERSUB]
        ax.plot(OVERSUB, ys, "o-", label=f"{MECHS[m].label.split(' (')[0]} reclaimable",
                color=MECH_COLORS[m], lw=2, ms=6)
    ax.set_xscale("log", base=2); ax.set_xticks(OVERSUB)
    ax.get_xaxis().set_major_formatter(plt.matplotlib.ticker.ScalarFormatter())
    ax.set_xlabel("Oversubscription (x)")
    ax.set_ylabel("Reclaimable capacity (% of pool)")
    ax.set_title("(c) Reclaimable capacity under load")
    ax.set_ylim(-5, 105)
    ax.legend(fontsize=8, loc="center right"); ax.grid(alpha=0.3)

    fig.suptitle(f"Optimistic Reclaim under Extreme Oversubscription "
                 f"(bound: {bound_mode}, {tenants} tenants, pool={CAPACITY})",
                 fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(OUT / f"fig_optimistic_reclaim_{bound_mode}.{ext}", dpi=150,
                    bbox_inches="tight")
    plt.close(fig)


def write_outputs(all_rows: List[Dict], agg: Dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # raw CSV
    import csv
    cols = ["bound_mode", "mechanism", "oversubscription", "n_tenants", "seed",
            "valid_throughput_Bpns", "admission_p99_ns", "backlog_wait_p99_ns",
            "pinned_peak", "pinned_byte_time_Bns", "reclaimable_capacity_frac",
            "nonreclaimable_frac", "rpe_payload_frac", "blocked_reclaim_ratio"]
    with open(OUT / "optimistic_reclaim.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    # aggregated JSON (paper-facing)
    agg_json = {"|".join(map(str, k)): v for k, v in agg.items()}
    (OUT / "optimistic_reclaim.json").write_text(json.dumps(agg_json, indent=2))

    # 32x / 64x summary table (16 tenants, capacity bound)
    lines = []
    lines.append("Optimistic Reclaim — key differences at extreme oversubscription")
    lines.append(f"(16 tenants, pool={CAPACITY}, budget={BUDGET}, mean of {len(SEEDS)} seeds, bound=capacity)")
    lines.append("")
    hdr = f"{'oversub':>8} {'mechanism':>9} {'valid_thr':>10} {'P99_admit':>12} {'pinned_pk':>10} {'reclaim%':>9} {'RPE%':>6}"
    for oversub in [32, 64]:
        lines.append(f"--- {oversub}x oversubscription ---")
        lines.append(hdr)
        base = agg[("capacity", "PROSE", oversub, 16)]["valid_throughput_Bpns"]
        for m in MECH_ORDER:
            a = agg[("capacity", m, oversub, 16)]
            thr = a["valid_throughput_Bpns"] / base if base else 0.0
            lines.append(f"{oversub:>7}x {m:>9} {thr:>9.2f}x "
                         f"{a['admission_p99_ns']/1000:>10.1f}us "
                         f"{a['pinned_peak']:>10.0f} "
                         f"{a['reclaimable_capacity_frac']*100:>8.1f}% "
                         f"{a['rpe_payload_frac']*100:>5.1f}%")
        lines.append("")
    txt = "\n".join(lines)
    (OUT / "optimistic_reclaim_summary.txt").write_text(txt + "\n")
    print(txt)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bound-mode", choices=BOUND_MODES + ["both"], default="both")
    args = ap.parse_args()
    modes = BOUND_MODES if args.bound_mode == "both" else [args.bound_mode]

    all_rows: List[Dict] = []
    for bm in modes:
        print(f"[optimistic-reclaim] running grid for bound_mode={bm} ...")
        rows = run_grid(bm)
        all_rows.extend(rows)

    agg = aggregate(all_rows)
    write_outputs(all_rows, agg)
    for bm in modes:
        make_figure(agg, bm)
        print(f"  wrote fig_optimistic_reclaim_{bm}.pdf/png")
    print(f"\nOutputs in {OUT}")


if __name__ == "__main__":
    main()
