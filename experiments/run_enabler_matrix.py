#!/usr/bin/env python3
"""Enabler matrix — the 2x2 commit-gate x pool-autonomy headline experiment.

The revised ENABLER claim: budget enforcement (admission budgeting) could not
be SAFELY DEPLOYED under autonomous reclamation without the commit-time gate
(validate-and-hold at admission); the contract makes it deployable.  This
driver proves it by holding the admission-budget policy FIXED in all four
cells (same candidate sets, same 32-object admit budget, 16 tenants, 512-slot
pool, 32x candidate oversubscription) and toggling ONLY:

    rows: commit-time gate   ON / OFF
    cols: pool autonomy      ON / OFF   (endpoint autonomously evicts/reuses
                                          slots; OFF = slots never recycled)

    gate ON  + autonomy ON   = PROSE operating point        -> deployable
    gate OFF + autonomy ON   = budget enforcement without protection
                               -> undeployable: RPE > 0 (GENONLY-class ~17%)
    gate ON  + autonomy OFF  = safe but the pool cannot reclaim
                               -> undeployable: no reclaim (throughput collapse)
    gate OFF + autonomy OFF  = unprotected static pool (RPE == 0 only because
                               nothing is ever reused; same capacity collapse)

Paired comparison: every cell replays the IDENTICAL offered-load trajectory
per seed (generate_oversub_trace), via replay_enabler_cell in
experiments/oversub_reclaim.py.

Outputs:
  experiments/out/enabler_matrix/enabler_matrix.json   full per-seed + aggregate
  experiments/out/enabler_matrix/fig_enabler_matrix.{pdf,png}  2x2 headline figure
  results/enabler_matrix.json                          curated paper-facing summary
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean, stdev
from typing import Dict, List

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.oversub_reclaim import (           # noqa: E402
    OversubConfig, generate_oversub_trace, replay_enabler_cell,
)

OUT = ROOT / "experiments" / "out" / "enabler_matrix"
CURATED = ROOT / "results" / "enabler_matrix.json"

CELLS = [  # (key, gate_on, autonomy_on, row_label, col_label)
    ("gateON_autoON", True, True),
    ("gateON_autoOFF", True, False),
    ("gateOFF_autoON", False, True),
    ("gateOFF_autoOFF", False, False),
]
DEPLOYABLE_CELL = "gateON_autoON"

# t_{0.975, 9} for 95% CI over 10 seeds
T95 = {2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.262}


def ci95(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    t = T95.get(n, 1.96)
    return float(t * stdev(xs) / np.sqrt(n))


def run_matrix(seeds: List[int], oversub: int, tenants: int, budget: int,
               capacity: int, n_steps: int, lifetime: float) -> Dict[str, List[Dict]]:
    rows: Dict[str, List[Dict]] = {k: [] for k, _, _ in CELLS}
    for seed in seeds:
        cfg = OversubConfig(oversubscription=oversub, n_tenants=tenants,
                            admit_budget=budget, n_steps=n_steps,
                            capacity=capacity, token_table=capacity,
                            bound_mode="capacity", seed=seed)
        trace = generate_oversub_trace(cfg)
        for key, gate, aut in CELLS:
            rows[key].append(replay_enabler_cell(trace, gate, aut,
                                                 lifetime_mean_steps=lifetime))
    return rows


def aggregate(rows: Dict[str, List[Dict]]) -> Dict[str, Dict]:
    """Mean + 95% CI over seeds; sustained throughput normalized per-seed
    (paired) to the gate-ON + autonomy-ON cell."""
    agg: Dict[str, Dict] = {}
    prose_sustained = [r["sustained_valid_Bpstep"] for r in rows[DEPLOYABLE_CELL]]
    for key, _, _ in CELLS:
        rs = rows[key]
        sust_norm = [r["sustained_valid_Bpstep"] / p if p > 0 else 0.0
                     for r, p in zip(rs, prose_sustained)]
        rpe = [r["rpe_payload_frac"] for r in rs]
        recl = [r["reclaimable_frac_mean"] for r in rs]
        exhaust = [r["exhaustion_step"] for r in rs if r["exhaustion_step"] >= 0]
        agg[key] = {
            "sustained_throughput_norm": float(mean(sust_norm)),
            "sustained_throughput_norm_ci95": ci95(sust_norm),
            "sustained_valid_Bpstep": float(mean(r["sustained_valid_Bpstep"] for r in rs)),
            "rpe_payload_frac": float(mean(rpe)),
            "rpe_payload_frac_ci95": ci95(rpe),
            "reclaimable_frac_mean": float(mean(recl)),
            "reclaimable_frac_mean_ci95": ci95(recl),
            "exhaustion_step_mean": float(mean(exhaust)) if exhaust else None,
            "exhaustion_step_ci95": ci95(exhaust) if len(exhaust) > 1 else 0.0,
            "exhausted_in_seeds": f"{len(exhaust)}/{len(rs)}",
            "n_admitted_mean": float(mean(r["n_admitted"] for r in rs)),
            "n_failed_admissions_mean": float(mean(r["n_failed_admissions"] for r in rs)),
            "rpe_events_mean": float(mean(r["rpe_events"] for r in rs)),
            "evict_blocked_mean": float(mean(r["evict_blocked"] for r in rs)),
        }
    return agg


def acceptance_checks(agg: Dict[str, Dict]) -> Dict[str, Dict]:
    """The three headline acceptance checks (report actuals, no tuning)."""
    unprot = agg["gateOFF_autoON"]
    deployable = [k for k, _, _ in CELLS
                  if agg[k]["rpe_payload_frac"] == 0.0
                  and agg[k]["sustained_throughput_norm"] >= 0.99]
    off_cells = ["gateON_autoOFF", "gateOFF_autoOFF"]
    return {
        "A1_gateOFF_autoON_rpe_positive": {
            "pass": unprot["rpe_payload_frac"] > 0.0,
            "actual_rpe_payload_frac": unprot["rpe_payload_frac"],
            "target": "~0.17 (GENONLY-class at 32x)",
        },
        "A2_exactly_one_deployable_cell": {
            "pass": deployable == [DEPLOYABLE_CELL],
            "actual_deployable_cells": deployable,
            "criterion": "RPE == 0 AND sustained throughput >= 0.99x",
        },
        "A3_autonomyOFF_throughput_collapse": {
            "pass": all(agg[k]["sustained_throughput_norm"] <= 0.5
                        and agg[k]["exhaustion_step_mean"] is not None
                        for k in off_cells),
            "actual_sustained_norm": {k: agg[k]["sustained_throughput_norm"]
                                      for k in off_cells},
            "actual_exhaustion_step": {k: agg[k]["exhaustion_step_mean"]
                                       for k in off_cells},
            "criterion": ">= 50% sustained-throughput loss after exhaustion",
        },
    }


def mean_series(rows: Dict[str, List[Dict]]) -> Dict[str, Dict]:
    """Per-cell seed-mean time series (valid bytes/step, cumulative stale
    bytes, cumulative-mean reclaimable fraction)."""
    out: Dict[str, Dict] = {}
    for key, _, _ in CELLS:
        valid = np.mean([r["step_valid_bytes"] for r in rows[key]], axis=0)
        stale_cum = np.cumsum(np.mean([r["step_stale_bytes"] for r in rows[key]], axis=0))
        recl_step = np.mean([r["step_reclaimable_frac"] for r in rows[key]], axis=0)
        recl_cummean = np.cumsum(recl_step) / (np.arange(len(recl_step)) + 1)
        out[key] = {
            "step_valid_bytes": [float(v) for v in valid],
            "cum_stale_bytes": [float(v) for v in stale_cum],
            "cum_reclaimable_frac": [float(v) for v in recl_cummean],
        }
    return out


def make_figure(agg: Dict[str, Dict], series: Dict[str, Dict], n_steps: int,
                budget_bytes: float, n_seeds: int) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(12.5, 7.6), sharex=True)
    steps = np.arange(n_steps)
    status = {
        "gateON_autoON": ("DEPLOYABLE", "#1b9e77"),
        "gateOFF_autoON": ("UNDEPLOYABLE: RPE > 0", "#d95f02"),
        "gateON_autoOFF": ("UNDEPLOYABLE: no reclaim", "#c02828"),
        "gateOFF_autoOFF": ("UNDEPLOYABLE: no reclaim", "#c02828"),
    }
    for key, gate, aut in CELLS:
        ax = axes[0 if gate else 1][0 if aut else 1]
        valid_norm = np.asarray(series[key]["step_valid_bytes"]) / budget_bytes
        recl = np.asarray(series[key]["cum_reclaimable_frac"])
        stale_mb = np.asarray(series[key]["cum_stale_bytes"]) / 1e6
        ax.plot(steps, valid_norm, color="#1f78b4", lw=1.8,
                label="valid throughput /step")
        ax.plot(steps, recl, color="#555555", lw=1.4, ls="--",
                label="reclaimable frac (cum. mean)")
        ax.set_ylim(-0.05, 1.1)
        ax.grid(alpha=0.3)
        ax2 = ax.twinx()
        ax2.plot(steps, stale_mb, color="#d95f02", lw=1.2, alpha=0.85,
                 label="cum. stale bytes")
        ax2.set_ylim(bottom=0)
        ax2.set_ylabel("cumulative stale (MB)", fontsize=8, color="#d95f02")
        ax2.tick_params(axis="y", labelsize=7, colors="#d95f02")
        a = agg[key]
        label, color = status[key]
        txt = (f"sustained {a['sustained_throughput_norm']:.2f}x   "
               f"RPE {a['rpe_payload_frac'] * 100:.1f}%   "
               f"reclaimable {a['reclaimable_frac_mean'] * 100:.0f}%")
        if a["exhaustion_step_mean"] is not None:
            txt += f"\npool exhausted @ step {a['exhaustion_step_mean']:.0f}"
        ax.text(0.03, 0.03, txt, transform=ax.transAxes, fontsize=8,
                va="bottom", ha="left",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#888888", alpha=0.9))
        ax.text(0.5, 0.965, label, transform=ax.transAxes, fontsize=10,
                fontweight="bold", va="top", ha="center", color="white",
                bbox=dict(boxstyle="round,pad=0.32", fc=color, ec="none"))
        for spine in ax.spines.values():
            spine.set_edgecolor(color)
            spine.set_linewidth(2.5)
        if ax.get_subplotspec().is_first_row():
            ax.set_title(f"pool autonomy {'ON' if aut else 'OFF'}",
                         fontsize=11, pad=30)
        if ax.get_subplotspec().is_first_col():
            ax.set_ylabel(f"gate {'ON' if gate else 'OFF'}\n"
                          "per-step (norm.)", fontsize=9)
        ax.set_xlabel("decode step")
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="center right")

    fig.suptitle("The commit-time gate ENABLES budget enforcement under autonomous "
                 "reclamation\n(fixed 32-object admit budget in all cells; 16 tenants, "
                 f"512-slot pool, 32x oversubscription, mean of {n_seeds} seeds)",
                 fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(OUT / f"fig_enabler_matrix.{ext}", dpi=150, bbox_inches="tight")
    plt.close(fig)


def _per_seed_rows(rows: Dict[str, List[Dict]]) -> List[Dict]:
    out: List[Dict] = []
    for key, _, _ in CELLS:
        for r in rows[key]:
            out.append({
                "cell": key, "seed": r["seed"],
                "gate_on": r["gate_on"], "autonomy_on": r["autonomy_on"],
                "valid_bytes": r["valid_bytes"], "stale_bytes": r["stale_bytes"],
                "rpe_payload_frac": r["rpe_payload_frac"],
                "sustained_valid_Bpstep": r["sustained_valid_Bpstep"],
                "reclaimable_frac_mean": r["reclaimable_frac_mean"],
                "exhaustion_step": r["exhaustion_step"],
                "n_admitted": r["n_admitted"],
                "n_failed_admissions": r["n_failed_admissions"],
                "rpe_events": r["rpe_events"],
                "evict_blocked": r["evict_blocked"],
            })
    return out


def print_table(agg: Dict[str, Dict], checks: Dict[str, Dict]) -> None:
    hdr = (f"{'cell':<16} {'sustained(norm)':>22} {'RPE_payload':>18} "
           f"{'reclaimable':>18} {'exhaust@':>10}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for key, _, _ in CELLS:
        a = agg[key]
        ex = f"{a['exhaustion_step_mean']:.1f}" if a["exhaustion_step_mean"] is not None else "-"
        print(f"{key:<16} "
              f"{a['sustained_throughput_norm']:>8.3f} +/- {a['sustained_throughput_norm_ci95']:<8.3f} "
              f"{a['rpe_payload_frac']:>7.4f} +/- {a['rpe_payload_frac_ci95']:<8.4f} "
              f"{a['reclaimable_frac_mean']:>7.3f} +/- {a['reclaimable_frac_mean_ci95']:<8.3f} "
              f"{ex:>10}")
    print("\nAcceptance checks:")
    for name, c in checks.items():
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {name}: {c}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--steps", type=int, default=200)
    ap.add_argument("--oversub", type=int, default=32)
    ap.add_argument("--tenants", type=int, default=16)
    ap.add_argument("--budget", type=int, default=32)
    ap.add_argument("--capacity", type=int, default=512)
    ap.add_argument("--lifetime", type=float, default=8.0,
                    help="mean live span of a promoted object (decode steps)")
    ap.add_argument("--no-fig", action="store_true")
    args = ap.parse_args()

    seeds = list(range(args.seeds))
    print(f"[enabler-matrix] 4 cells x {len(seeds)} seeds "
          f"(oversub={args.oversub}x, tenants={args.tenants}, "
          f"budget={args.budget}, pool={args.capacity}, steps={args.steps})")
    rows = run_matrix(seeds, args.oversub, args.tenants, args.budget,
                      args.capacity, args.steps, args.lifetime)
    agg = aggregate(rows)
    checks = acceptance_checks(agg)
    series = mean_series(rows)
    print_table(agg, checks)

    config = {
        "seeds": seeds, "n_steps": args.steps, "oversubscription": args.oversub,
        "n_tenants": args.tenants, "admit_budget": args.budget,
        "capacity": args.capacity, "lifetime_mean_steps": args.lifetime,
        "cells": {k: {"gate_on": g, "autonomy_on": a} for k, g, a in CELLS},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    full = {"config": config, "aggregate": agg, "acceptance_checks": checks,
            "per_seed": _per_seed_rows(rows), "time_series": series}
    (OUT / "enabler_matrix.json").write_text(json.dumps(full, indent=2))

    CURATED.parent.mkdir(parents=True, exist_ok=True)
    curated = {"config": config, "aggregate": agg, "acceptance_checks": checks}
    CURATED.write_text(json.dumps(curated, indent=2))

    if not args.no_fig:
        from experiments.baselines.baseline_common import BaselineConfig
        budget_bytes = args.budget * float(BaselineConfig().object_bytes)
        make_figure(agg, series, args.steps, budget_bytes, len(seeds))
        print(f"\nFigure: {OUT}/fig_enabler_matrix.pdf/.png")
    print(f"JSON:   {OUT}/enabler_matrix.json\nCurated: {CURATED}")

    if not all(c["pass"] for c in checks.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
