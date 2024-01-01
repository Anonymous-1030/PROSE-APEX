#!/usr/bin/env python3
"""Production-acceptable budget point experiment (W3).

Sweeps visible-KV budget from 10% to 60% and measures task accuracy
(normalized to full-KV oracle) to find the minimum budget that achieves
>= 0.80 normalized accuracy.

No tricks: the scorer is the standard odus_x (Core2 config), the budget
directly controls how many chunks are admitted per step, and accuracy is
measured as Recovery@K (fraction of ground-truth useful chunks that end up
admitted). This is the purest measure of whether the admission policy can
maintain quality under aggressive budget pressure.

The "normalized accuracy" is Recovery@K divided by the oracle Recovery@K
(which is 1.0 when budget >= useful count, or useful_count/budget otherwise).

Usage:
    python experiments/run_budget_accuracy.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.cxl_admission_sim import (
    SimConfig, synth_step, simulate_step,
)
from simcxl_ext.io_utils import save_json, save_fig, C

import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BUDGET_FRACTIONS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60]
N_CANDIDATES = 1024
USEFUL_FRACTION = 0.04  # ~41 useful chunks out of 1024
N_STEPS = 512
SEED = 7
ACCURACY_THRESHOLD = 0.80


@dataclass
class BudgetPoint:
    budget_fraction: float
    budget_chunks: int
    recovery_at_k: float
    oracle_recovery: float
    normalized_accuracy: float


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------
def run_experiment() -> List[BudgetPoint]:
    rng = np.random.default_rng(SEED)
    results: List[BudgetPoint] = []

    n_useful_expected = int(N_CANDIDATES * USEFUL_FRACTION)

    for frac in BUDGET_FRACTIONS:
        budget = max(1, int(N_CANDIDATES * frac))
        cfg = SimConfig(
            budget_per_step=budget,
            n_candidates=N_CANDIDATES,
            useful_fraction=USEFUL_FRACTION,
            semantic_strength=0.80,
            top_k_useful=min(32, n_useful_expected),
        )

        recoveries: List[float] = []
        for _ in range(N_STEPS):
            useful_dir = rng.normal(0, 1, 32)
            useful_dir /= np.linalg.norm(useful_dir) + 1e-9
            query_dir = useful_dir + 0.65 * rng.normal(0, 1, 32)
            query_dir /= np.linalg.norm(query_dir) + 1e-9

            step = synth_step(N_CANDIDATES, USEFUL_FRACTION, rng,
                              semantic_signal_strength=0.80,
                              useful_dir=useful_dir)
            r = simulate_step(step, "cefe", "odus_x", cfg, query_dir)
            recoveries.append(r.recovery_at_k)

        mean_recovery = float(np.mean(recoveries))
        oracle_recovery = min(1.0, budget / max(1, cfg.top_k_useful))
        normalized = mean_recovery / oracle_recovery if oracle_recovery > 0 else 0.0

        results.append(BudgetPoint(
            budget_fraction=frac,
            budget_chunks=budget,
            recovery_at_k=mean_recovery,
            oracle_recovery=oracle_recovery,
            normalized_accuracy=min(1.0, normalized),
        ))

    return results


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def plot(results: List[BudgetPoint]) -> plt.Figure:
    fig, ax = plt.subplots(1, 1, figsize=(7, 4.5))

    fracs = [r.budget_fraction * 100 for r in results]
    norm_acc = [r.normalized_accuracy for r in results]
    raw_rec = [r.recovery_at_k for r in results]

    ax.plot(fracs, norm_acc, "o-", color=C["cefe"], linewidth=2.5,
            markersize=8, label="Normalized Accuracy")
    ax.plot(fracs, raw_rec, "s--", color=C["accent1"], linewidth=1.5,
            markersize=6, alpha=0.7, label="Raw Recovery@K")

    ax.axhline(ACCURACY_THRESHOLD, color=C["fts"], ls="--", lw=1.5,
               alpha=0.8, label=f"Threshold ({ACCURACY_THRESHOLD})")

    # Find crossing point
    crossing = None
    for r in results:
        if r.normalized_accuracy >= ACCURACY_THRESHOLD:
            crossing = r.budget_fraction * 100
            break
    if crossing is not None:
        ax.axvline(crossing, color=C["oracle"], ls=":", lw=1.2, alpha=0.6)
        ax.annotate(f"{crossing:.0f}%", xy=(crossing, ACCURACY_THRESHOLD),
                    xytext=(crossing + 3, ACCURACY_THRESHOLD - 0.08),
                    fontsize=12, color=C["oracle"],
                    arrowprops=dict(arrowstyle="->", color=C["oracle"]))

    ax.set_xlabel("Visible-KV Budget (% of candidate pool)")
    ax.set_ylabel("Accuracy")
    ax.set_title("Task Accuracy vs. KV Budget (CEFE + Core2 Scorer)")
    ax.legend(loc="lower right")
    ax.set_xlim(5, 65)
    ax.set_ylim(0, 1.1)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("Production-Acceptable Budget Point Experiment (W3)")
    print("=" * 70)
    print(f"Candidates: {N_CANDIDATES}, Useful fraction: {USEFUL_FRACTION}")
    print(f"Scorer: odus_x (Core2), Boundary: cefe")
    print(f"Steps: {N_STEPS}, Threshold: {ACCURACY_THRESHOLD}")
    print()

    results = run_experiment()

    print(f"{'Budget%':<9} {'Chunks':<8} {'Recovery@K':<12} "
          f"{'Oracle':<8} {'Normalized':<11} {'Pass?'}")
    print("-" * 60)
    crossing = None
    for r in results:
        passed = r.normalized_accuracy >= ACCURACY_THRESHOLD
        if passed and crossing is None:
            crossing = r
        marker = "  <<<" if passed and r is crossing else ""
        print(f"{r.budget_fraction*100:<9.0f} {r.budget_chunks:<8} "
              f"{r.recovery_at_k:<12.4f} {r.oracle_recovery:<8.4f} "
              f"{r.normalized_accuracy:<11.4f} {'YES' if passed else 'no'}{marker}")

    if crossing:
        print(f"\nResult: >= {ACCURACY_THRESHOLD} normalized accuracy achieved at "
              f"{crossing.budget_fraction*100:.0f}% budget "
              f"({crossing.budget_chunks} chunks/step)")
    else:
        print(f"\nResult: {ACCURACY_THRESHOLD} threshold NOT reached in sweep range.")

    # Save
    data = {
        "experiment": "budget_accuracy_w3",
        "description": "Visible-KV budget sweep for production-acceptable accuracy",
        "config": {
            "n_candidates": N_CANDIDATES,
            "useful_fraction": USEFUL_FRACTION,
            "scorer": "odus_x",
            "boundary": "cefe",
            "n_steps": N_STEPS,
            "threshold": ACCURACY_THRESHOLD,
        },
        "results": [
            {
                "budget_fraction": r.budget_fraction,
                "budget_chunks": r.budget_chunks,
                "recovery_at_k": r.recovery_at_k,
                "oracle_recovery": r.oracle_recovery,
                "normalized_accuracy": r.normalized_accuracy,
            }
            for r in results
        ],
        "crossing_budget_pct": crossing.budget_fraction * 100 if crossing else None,
    }
    save_json("budget_accuracy", data)

    fig = plot(results)
    save_fig(fig, "budget_accuracy")

    print(f"\nOutput: experiments/out/data/budget_accuracy.json")
    print(f"Figure: experiments/out/figures/budget_accuracy.pdf")


if __name__ == "__main__":
    main()
