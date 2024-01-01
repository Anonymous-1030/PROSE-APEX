#!/usr/bin/env python3
r"""Compact, publication-grade sensitivity_enclosure figure.

Designed so that when embedded at \\columnwidth (3.45 in) in LaTeX,
all text is legible WITHOUT zooming (minimum ~8 pt after embedding).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch

# Path setup
_PKG_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = _PKG_ROOT / "experiments" / "out" / "data" / "sensitivity_enclosure.json"
FIG_DIR = _PKG_ROOT / "experiments" / "out" / "figures"

# ---------------------------------------------------------------------------
# Aesthetic constants
# ---------------------------------------------------------------------------
COL_BASE = "#2563EB"
COL_BEST = "#059669"
COL_WORST = "#DC2626"
COL_CORNER = "#7C3AED"
COL_BAR = "#94A3B8"
COL_TEXT = "#1E293B"
COL_GRID = "#E2E8F0"
COL_BUDGET = "#B91C1C"  # darker red for budget lines

FIG_W = 3.45
FIG_H = 2.4

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["DejaVu Serif", "Times New Roman", "serif"],
    "font.size": 8,
    "axes.titlesize": 9,
    "axes.labelsize": 8,
    "xtick.labelsize": 7,
    "ytick.labelsize": 8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "axes.linewidth": 0.6,
    "lines.linewidth": 1.2,
    "figure.dpi": 300,
})


def load_data():
    with DATA_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def add_range_bar(ax, y, mn, mx, height=0.5, color=COL_BAR, alpha=0.25):
    width = mx - mn
    if width < 1e-6:
        return
    patch = FancyBboxPatch(
        (mn, y - height / 2), width, height,
        boxstyle=f"round,pad=0,rounding_size={height / 3:.4f}",
        facecolor=color, edgecolor=color,
        linewidth=0.7, alpha=alpha, zorder=2,
    )
    ax.add_patch(patch)


LABEL_SHORT = {
    "Tenants (1-32)": "Tenants",
    "DMA/arb (0.25x-8x)": "DMA/arb",
    "ABA threshold (0.50-0.95)": "ABA thresh.",
    "Descriptors (256-4096)": "Descriptors",
    "P2P write (+0-1 us)": "P2P write",
    "CFO overlap (0-50%)": "CFO overlap",
    "CXL BW (2-64 GB/s)": "CXL BW",
}


def plot_beautified(data):
    fig = plt.figure(figsize=(FIG_W, FIG_H))

    gs = fig.add_gridspec(
        1, 2,
        width_ratios=[1.0, 1.0],
        wspace=0.9,
        left=0.18, right=0.96,
        top=0.75, bottom=0.20,
    )
    ax_l = fig.add_subplot(gs[0, 0])
    ax_r = fig.add_subplot(gs[0, 1])

    for ax in (ax_l, ax_r):
        ax.tick_params(axis="y", length=0, pad=4)
        ax.tick_params(axis="x", length=3, pad=2)
        ax.grid(axis="x", linestyle="-", linewidth=0.4,
                color=COL_GRID, alpha=0.9, zorder=0)
        ax.set_axisbelow(True)

    # -------------------------------------------------------------------
    # Left panel: Timing sensitivity
    # -------------------------------------------------------------------
    lat_keys = ["tenants", "dma_arb", "aba_threshold"]
    lat_entries = []
    for key in lat_keys:
        d = data[key]
        p99s = [pt["p99"] for pt in d["points"]]
        base = next(pt["p99"] for pt in d["points"] if pt["x"] == d["baseline_x"])
        lat_entries.append((d["label"], min(p99s), base, max(p99s)))

    n_lat = len(lat_entries)
    for i, (lbl, mn, base, mx) in enumerate(lat_entries):
        y = n_lat - 1 - i
        add_range_bar(ax_l, y, mn, mx)
        ax_l.plot(mn, y, "|", color=COL_BEST, ms=7, mew=1.5, zorder=4)
        ax_l.plot(mx, y, "|", color=COL_WORST, ms=7, mew=1.5, zorder=4)
        ax_l.plot(base, y, "D", color=COL_BASE, ms=4, zorder=5)

    # Pessimistic corner — between top bar and top of axes
    cw_p99 = data["combined_worst"]["p99_ns"]
    ax_l.plot(cw_p99, n_lat - 1, "*", color=COL_CORNER, ms=9, zorder=6,
              markeredgecolor="white", markeredgewidth=0.4)

    # 1 µs budget line (no text label — explained in legend)
    ax_l.axvline(1000, color=COL_BUDGET, ls="--", lw=1.0, alpha=0.85, zorder=3)

    ax_l.set_yticks(range(n_lat))
    ax_l.set_yticklabels([LABEL_SHORT.get(e[0], e[0]) for e in lat_entries][::-1])
    ax_l.set_xlabel("P99 Latency (ns)", labelpad=3)
    ax_l.set_xlim(0, 1100)
    ax_l.set_ylim(-0.6, n_lat - 0.5)
    ax_l.set_title("(a) Timing", fontweight="bold", pad=4)

    # -------------------------------------------------------------------
    # Right panel: Throughput sensitivity
    # -------------------------------------------------------------------
    spd_keys = ["candidates", "p2p_latency", "cfo_overlap", "cxl_bw"]
    spd_entries = []
    for key in spd_keys:
        d = data[key]
        spds = [pt["speedup"] for pt in d["points"]]
        base = next(pt["speedup"] for pt in d["points"] if pt["x"] == d["baseline_x"])
        spd_entries.append((d["label"], min(spds), base, max(spds)))

    n_spd = len(spd_entries)
    for i, (lbl, mn, base, mx) in enumerate(spd_entries):
        y = n_spd - 1 - i
        add_range_bar(ax_r, y, mn, mx)
        ax_r.plot(mn, y, "|", color=COL_WORST, ms=7, mew=1.5, zorder=4)
        ax_r.plot(mx, y, "|", color=COL_BEST, ms=7, mew=1.5, zorder=4)
        ax_r.plot(base, y, "D", color=COL_BASE, ms=4, zorder=5)

    # Pessimistic corner
    cw_spd = data["combined_worst"]["speedup_vs_fts"]
    ax_r.plot(cw_spd, n_spd - 1, "*", color=COL_CORNER, ms=9, zorder=6,
              markeredgecolor="white", markeredgewidth=0.4)

    # FTS parity line (no text label — explained in legend)
    ax_r.axvline(1.0, color=COL_BUDGET, ls="--", lw=1.0, alpha=0.85, zorder=3)

    ax_r.set_yticks(range(n_spd))
    ax_r.set_yticklabels([LABEL_SHORT.get(e[0], e[0]) for e in spd_entries][::-1])
    ax_r.set_xlabel("Speedup vs FTS (×)", labelpad=3)
    ax_r.set_xlim(0.3, 12.5)
    ax_r.set_ylim(-0.6, n_spd - 0.5)
    ax_r.set_title("(b) Throughput", fontweight="bold", pad=4)

    # -------------------------------------------------------------------
    # Shared legend — all semantics in one place, no in-plot text needed
    # -------------------------------------------------------------------
    legend_elements = [
        Line2D([0], [0], marker="D", color="w", markerfacecolor=COL_BASE,
               ms=4.5, label="Baseline"),
        Line2D([0], [0], marker="|", color=COL_BEST, ms=7, mew=1.5,
               linestyle="None", label="Best OAT"),
        Line2D([0], [0], marker="|", color=COL_WORST, ms=7, mew=1.5,
               linestyle="None", label="Worst OAT"),
        Line2D([0], [0], marker="*", color=COL_CORNER, ms=8,
               linestyle="None", label="Pessimistic corner"),
        Line2D([0], [0], color=COL_BUDGET, ls="--", lw=1.0,
               label="Budget (1 μs / FTS parity)"),
    ]
    fig.legend(
        handles=legend_elements, fontsize=6.5, loc="upper center",
        ncol=3, frameon=False, bbox_to_anchor=(0.57, 0.97),
        handletextpad=0.4, columnspacing=1.0,
    )

    fig.suptitle(
        "Sensitivity — PROSE Endpoint Parameters",
        fontsize=9.5, fontweight="bold", color=COL_TEXT, y=1.02,
    )
    return fig


def main():
    if not DATA_PATH.exists():
        print(f"ERROR: data file not found: {DATA_PATH}")
        print("Run experiments/run_sensitivity_enclosure.py first.")
        sys.exit(1)

    data = load_data()
    fig = plot_beautified(data)

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = FIG_DIR / "sensitivity_enclosure_beautified.pdf"
    png_path = FIG_DIR / "sensitivity_enclosure_beautified.png"

    fig.savefig(pdf_path, bbox_inches="tight", pad_inches=0.02,
                facecolor="white")
    fig.savefig(png_path, bbox_inches="tight", pad_inches=0.02,
                facecolor="white", dpi=300)
    plt.close(fig)

    print(f"Saved beautified figure to:\n  {pdf_path}\n  {png_path}")


if __name__ == "__main__":
    main()
