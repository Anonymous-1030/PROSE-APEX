#!/usr/bin/env python3
"""Reproduce: the win-region phase diagrams (paper Section, Scope/Regime).

Purpose
  Answer the "cherry-picked operating point" critique head on. Instead of
  reporting a single favourable point, we sweep the deployment envelope and
  show where PROSE-APEX (CEFE endpoint admission) wins over the fetch-then-score
  (FTS) upper bound, and where it collapses to parity. The speedup=1.0 contour
  is drawn so the reader can see that the parity region is the corner nobody
  provisions (single tenant, abundant bandwidth, no oversubscription).

Single panel
  speedup over (cxl_bw_gbs x oversubscription), the two knobs that actually
  drive the throughput gap. The bandwidth sweep is restricted to the valid
  2-8 GB/s range. The point (2 GB/s, 16x) reproduces the paper's headline
  3.1x; the high-oversubscription / low-bandwidth corner shows the collapse
  of FTS.

Why host count is not an axis here. Speedup is a ratio of steady-state
throughput, and adding hosts costs admission latency, fairness, and RPE
residual rather than steady throughput, so a (bw x hosts) map would show
identical rows and mislead. Host scaling is reported separately through the
RPE residual and Jain-fairness curves (Multi-Host Scalability). We fix hosts
at 16 (the contended production point) for the map below.

Speedup = tok_per_s(cefe / odus_x) / tok_per_s(fts_none / none), the unfiltered
upper bound, at the SAME SimConfig.

Self-contained: sweeps SimConfig knobs already present in cxl_admission_sim,
introduces no new model. Run:  python experiments/run_phase_diagram.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.cxl_admission_sim import run_closed_loop, SimConfig
from simcxl_ext.io_utils import save_json, save_fig, C

N_STEPS = 256
SEED = 0
BUDGET = 64  # admits per step, matches SimConfig.budget_per_step default

BW_AXIS = [2.0, 4.0, 8.0]                           # valid GB/s per-tenant range
OVERSUB_AXIS = [1, 2, 4, 8, 16, 32]                 # multiples of the HBM budget
FIXED_HOSTS = 16                                    # contended production point


def speedup(bw: float, n_candidates: int) -> float:
    common = dict(cxl_bw_gbs=bw, n_hosts=FIXED_HOSTS, n_candidates=n_candidates)
    cefe = run_closed_loop("cefe", "odus_x", SimConfig(**common),
                           n_steps=N_STEPS, seed=SEED)["tok_per_s_mean"]
    fts = run_closed_loop("fts_none", "none", SimConfig(**common),
                          n_steps=N_STEPS, seed=SEED)["tok_per_s_mean"]
    return cefe / max(fts, 1e-9)


def run() -> Dict:
    grid = np.zeros((len(OVERSUB_AXIS), len(BW_AXIS)))
    for i, ov in enumerate(OVERSUB_AXIS):
        for j, bw in enumerate(BW_AXIS):
            grid[i, j] = speedup(bw, ov * BUDGET)
    return {
        "bw_axis": BW_AXIS, "oversub_axis": OVERSUB_AXIS,
        "fixed_hosts": FIXED_HOSTS,
        "grid_oversub_bw": grid.tolist(),
        "n_steps": N_STEPS, "budget": BUDGET,
    }


def report(res: Dict) -> None:
    M = np.array(res["grid_oversub_bw"])
    print("=" * 70)
    print("Win-region phase diagram  (speedup CEFE / fetch-then-score)")
    print("=" * 70)
    print(f"rows=oversub {res['oversub_axis']}  cols=bw(GB/s) {res['bw_axis']}"
          f"  @ {res['fixed_hosts']} hosts")
    for i, ov in enumerate(res["oversub_axis"]):
        print(f"  {ov:>2}x: " + " ".join(f"{v:5.2f}" for v in M[i]))
    centre = M[res["oversub_axis"].index(16), res["bw_axis"].index(2.0)]
    print(f"Centre (2 GB/s, 16x) = {centre:.2f}x  (paper headline 3.1x)")
    print(f"Max speedup in sweep = {M.max():.2f}x ; min = {M.min():.2f}x")


def plot(res: Dict):
    import matplotlib.pyplot as plt
    M = np.array(res["grid_oversub_bw"])
    fig, ax = plt.subplots(figsize=(5.6, 4.0))
    im = ax.imshow(M, origin="lower", aspect="auto", cmap="viridis",
                   vmin=1.0, vmax=max(2.0, M.max()))
    ax.set_xticks(range(len(res["bw_axis"])))
    ax.set_xticklabels([f"{b:g}" for b in res["bw_axis"]])
    ax.set_yticks(range(len(res["oversub_axis"])))
    ax.set_yticklabels([f"{o}x" for o in res["oversub_axis"]])
    ax.set_xlabel("Per-tenant bandwidth (GB/s)")
    ax.set_ylabel("Candidate oversubscription")
    try:
        ax.contour(M, levels=[1.05], colors="white", linewidths=1.8,
                   linestyles="--")
    except Exception:
        pass
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            ax.text(j, i, f"{M[i,j]:.1f}", ha="center", va="center",
                    color="white" if M[i, j] < 0.6 * M.max() else "black",
                    fontsize=7)
    fig.colorbar(im, ax=ax, label="Speedup")
    fig.tight_layout()
    return fig


def main() -> None:
    res = run()
    report(res)
    save_json("phase_diagram", res)
    save_fig(plot(res), "fig_phase_diagram")
    print("\nSaved fig_phase_diagram + phase_diagram.json under experiments/out/")


if __name__ == "__main__":
    main()
