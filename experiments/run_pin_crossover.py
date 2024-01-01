#!/usr/bin/env python3
"""Pin-table crossover curve for P1-2.

For each pin-table size C in {512,1024,2048,4096}, run the SimCXL replay
and find the lowest oversubscription in a step-2 sweep at which 2PHASE's
peak pinned licenses reach C (i.e., the pool is exhausted).  Because the
replay pins the entire queued candidate set before admission, the resulting
structural threshold is C / admit_budget; the sweep is recorded so the
figure can overlay simulated markers on the analytical line honestly.

Outputs:
  experiments/out/pin_crossover/pin_crossover.json
  experiments/out/pin_crossover/fig_pin_crossover.{pdf,png}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.oversub_reclaim import (
    OversubConfig, generate_oversub_trace, replay_oversub, MECHS,
)

OUT = ROOT / "experiments" / "out" / "pin_crossover"
PIN_SIZES = [512, 1024, 2048, 4096]
OVERSUBS = list(range(2, 129, 2))   # 2,4,6,...,128
TENANTS = 16
BUDGET = 32
N_STEPS = 200
SEED = 0


def crossover_for_pin(pin_size: int, bound_mode: str = "token_table") -> int:
    """Return lowest oversub where 2PHASE peak pinned >= pin_size."""
    for oversub in OVERSUBS:
        cfg = OversubConfig(
            oversubscription=oversub, n_tenants=TENANTS,
            admit_budget=BUDGET, n_steps=N_STEPS,
            bound_mode=bound_mode, seed=SEED,
        )
        if bound_mode == "capacity":
            cfg.capacity = pin_size
        else:
            cfg.token_table = pin_size
        trace = generate_oversub_trace(cfg)
        r = replay_oversub(trace, MECHS["2PHASE"])
        if int(r["pinned_peak"]) >= pin_size:
            return oversub
    return OVERSUBS[-1]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for pin in PIN_SIZES:
        cross_cap = crossover_for_pin(pin, "capacity")
        cross_token = crossover_for_pin(pin, "token_table")
        results.append({
            "pin_entries": pin,
            "crossover_oversub_capacity": cross_cap,
            "crossover_oversub_token_table": cross_token,
        })

    json_path = OUT / "pin_crossover.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    # Plot
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif", "serif"],
        "font.size": 9,
        "axes.labelsize": 9,
        "axes.titlesize": 10,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "axes.linewidth": 0.9,
        "lines.linewidth": 1.2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": 150,
        "savefig.dpi": 300,
        "pdf.fonttype": 42,
    })
    fig, ax = plt.subplots(figsize=(3.4, 2.4))
    x = [r["pin_entries"] for r in results]
    y = [r["crossover_oversub_capacity"] for r in results]
    # Capacity and token-table models return identical crossovers; plot one marker series.
    ax.plot(x, y, "o", color="#1f77b4", label="Simulated crossover")
    # Analytical structural threshold: one pin per queued candidate.
    theor = [c / BUDGET for c in x]
    ax.plot(x, theor, ":", color="#7f7f7f", label="Analytical: entries / 32")
    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=2)
    ax.set_xticks(x)
    ax.set_xticklabels([str(c) for c in x])
    ax.set_xlabel("Pin / reservation entries")
    ax.set_ylabel("Oversubscription at pool exhaustion ($\\times$)")
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "fig_pin_crossover.pdf", bbox_inches="tight")
    fig.savefig(OUT / "fig_pin_crossover.png", bbox_inches="tight")
    plt.close(fig)

    print(json.dumps(results, indent=2))
    print(f"Wrote {json_path} and figures to {OUT}")


if __name__ == "__main__":
    main()
