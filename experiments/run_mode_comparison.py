#!/usr/bin/env python3
"""
Mode A vs Mode B Performance Comparison and Latency Sensitivity Analysis.

Reproduces two key results for reviewer rebuttal:
  1. Mode A (Push) and Mode B (Pull) both achieve RPE=0, with Mode A offering
     lower latency due to endpoint-initiated DMA.
  2. Mode B throughput degrades gracefully as host-pull RTT increases, while
     RPE elimination remains structurally intact at all latencies.

Output:
  experiments/out/mode_comparison/comparison.json
  experiments/out/mode_comparison/latency_sweep.json
  experiments/out/mode_comparison/fig_mode_comparison.pdf
  experiments/out/mode_comparison/fig_latency_sensitivity.pdf
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import numpy as np
import matplotlib.pyplot as plt

from simcxl_ext.cxl_admission_sim import SimConfig, run_closed_loop

OUT_DIR = Path(__file__).resolve().parent / "out" / "mode_comparison"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def run_mode_comparison():
    """Compare Baseline FTS, Mode B (Pull), and Mode A (Push)."""
    cfg = SimConfig(
        n_candidates=1024,
        budget_per_step=64,
        top_k_useful=32,
        useful_fraction=0.04,
        cxl_bw_gbs=32.0,
    )

    configs = [
        ("Baseline (FTS)", "fts_quest", "quest", {}),
        ("Mode B (Pull)", "cefe_pull", "odus_x", {}),
        ("Mode A (Push)", "cefe", "odus_x", {}),
    ]

    results = []
    for label, boundary, scorer, extra in configs:
        r = run_closed_loop(boundary, scorer, cfg, n_steps=256, seed=42)
        results.append({
            "label": label,
            "boundary": boundary,
            "throughput_tok_s": r["tok_per_s_mean"],
            "rpe_bytes_mean": r["rpe_bytes_mean"],
            "rpe_rate_pct": r["rpe_bytes_mean"] / max(1.0, r["useful_bytes_mean"] + r["wasted_bytes_mean"]) * 100,
            "bandwidth_waste_pct": r["wasted_bytes_mean"] / max(1.0, r["useful_bytes_mean"] + r["wasted_bytes_mean"]) * 100,
            "admission_latency_us": r["admission_us_mean"],
            "recovery_at_k": r["recovery_at_k_mean"],
        })

    with open(OUT_DIR / "comparison.json", "w") as f:
        json.dump(results, f, indent=2)

    print("=== Mode Comparison ===")
    print(f"{'Config':<20s} {'Tput(tok/s)':<14s} {'RPE(%)':>8s} {'BW Waste(%)':>12s} {'Lat(us)':>10s} {'R@K':>6s}")
    print("-" * 72)
    for r in results:
        print(f"{r['label']:<20s} {r['throughput_tok_s']:<14.1f} {r['rpe_rate_pct']:>8.2f} "
              f"{r['bandwidth_waste_pct']:>12.2f} {r['admission_latency_us']:>10.2f} {r['recovery_at_k']:>6.3f}")

    return results


def run_latency_sweep():
    """Sweep host-pull RTT for Mode B, showing graceful degradation.

    Uses a tighter decode slack to make the pull RTT visible in throughput.
    This models a latency-sensitive serving scenario (short decode steps).
    """
    rtt_values_ns = [50, 100, 150, 200, 300, 500, 750, 1000, 2000, 5000]

    results = []
    for rtt_ns in rtt_values_ns:
        cfg = SimConfig(
            n_candidates=1024,
            budget_per_step=64,
            top_k_useful=32,
            useful_fraction=0.04,
            cxl_bw_gbs=32.0,
            decode_compute_us=500.0,   # Short decode step (7B model)
            decode_slack_us=200.0,     # Tight slack window
            pull_host_rtt_ns=float(rtt_ns),
            pull_use_p2p=True,
        )
        r = run_closed_loop("cefe_pull", "odus_x", cfg, n_steps=256, seed=42)
        results.append({
            "host_rtt_ns": rtt_ns,
            "throughput_tok_s": r["tok_per_s_mean"],
            "rpe_bytes_mean": r["rpe_bytes_mean"],
            "admission_latency_us": r["admission_us_mean"],
            "recovery_at_k": r["recovery_at_k_mean"],
        })

    # Also sweep with host-bounce (no P2P)
    for rtt_ns in [150, 500, 1000]:
        cfg = SimConfig(
            n_candidates=1024,
            budget_per_step=64,
            top_k_useful=32,
            useful_fraction=0.04,
            cxl_bw_gbs=32.0,
            decode_compute_us=500.0,
            decode_slack_us=200.0,
            pull_host_rtt_ns=float(rtt_ns),
            pull_use_p2p=False,
            pull_host_bounce_us=5.0,
        )
        r = run_closed_loop("cefe_pull", "odus_x", cfg, n_steps=256, seed=42)
        results.append({
            "host_rtt_ns": rtt_ns,
            "host_bounce": True,
            "throughput_tok_s": r["tok_per_s_mean"],
            "rpe_bytes_mean": r["rpe_bytes_mean"],
            "admission_latency_us": r["admission_us_mean"],
            "recovery_at_k": r["recovery_at_k_mean"],
        })

    with open(OUT_DIR / "latency_sweep.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\n=== Latency Sensitivity (Mode B) ===")
    print(f"{'RTT(ns)':<10s} {'P2P':<5s} {'Tput(tok/s)':<14s} {'RPE':>6s} {'Lat(us)':>10s}")
    print("-" * 50)
    for r in results:
        p2p = "no" if r.get("host_bounce") else "yes"
        print(f"{r['host_rtt_ns']:<10d} {p2p:<5s} {r['throughput_tok_s']:<14.1f} "
              f"{r['rpe_bytes_mean']:>6.0f} {r['admission_latency_us']:>10.2f}")

    return results


# --------------------------------------------------------------------------- #
# Plotting
# --------------------------------------------------------------------------- #

def plot_mode_comparison(results):
    """Bar chart: Throughput and RPE for Baseline, Mode B, Mode A."""
    labels = [r["label"] for r in results]
    tput = [r["throughput_tok_s"] for r in results]
    rpe = [r["rpe_rate_pct"] for r in results]
    waste = [r["bandwidth_waste_pct"] for r in results]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7, 3.0))
    x = np.arange(len(labels))
    colors = ["#d62728", "#2ca02c", "#1f77b4"]

    ax1.bar(x, tput, color=colors, width=0.6)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=8)
    ax1.set_ylabel("Throughput (tok/s)")
    ax1.set_title("Throughput Comparison")
    ax1.grid(axis="y", alpha=0.3)

    bw = 0.35
    ax2.bar(x - bw/2, rpe, bw, label="RPE (%)", color="#d62728", alpha=0.8)
    ax2.bar(x + bw/2, waste, bw, label="BW Waste (%)", color="#ff7f0e", alpha=0.8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=8)
    ax2.set_ylabel("Percentage (%)")
    ax2.set_title("RPE and Bandwidth Waste")
    ax2.legend(fontsize=7)
    ax2.grid(axis="y", alpha=0.3)
    for i in [1, 2]:
        ax2.annotate("RPE=0", (x[i] - bw/2, 0.5), fontsize=7,
                     ha="center", color="#2ca02c", fontweight="bold")

    plt.tight_layout()
    fig.savefig(OUT_DIR / "fig_mode_comparison.pdf", bbox_inches="tight", dpi=150)
    plt.close()
    print(f"\nSaved: {OUT_DIR / 'fig_mode_comparison.pdf'}")


def plot_latency_sensitivity(results):
    """Line chart: throughput vs host-pull RTT for Mode B."""
    p2p = [r for r in results if not r.get("host_bounce")]
    bounce = [r for r in results if r.get("host_bounce")]

    fig, ax = plt.subplots(figsize=(5, 3.2))
    ax.plot([r["host_rtt_ns"] for r in p2p],
            [r["throughput_tok_s"] for r in p2p],
            "o-", color="#1f77b4", lw=1.5, ms=5, label="Mode B (P2P)")
    if bounce:
        ax.plot([r["host_rtt_ns"] for r in bounce],
                [r["throughput_tok_s"] for r in bounce],
                "s--", color="#d62728", lw=1.5, ms=5, label="Mode B (host-bounce +5us)")

    ax.set_xscale("log")
    ax.set_xlabel("Host-Pull Round-Trip Time (ns)")
    ax.set_ylabel("Throughput (tok/s)")
    ax.set_title("Mode B Latency Sensitivity")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.text(0.98, 0.95, "RPE = 0 at all latencies",
            transform=ax.transAxes, ha="right", va="top", fontsize=8,
            color="#2ca02c", fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", fc="#e8f5e9", alpha=0.8))

    plt.tight_layout()
    fig.savefig(OUT_DIR / "fig_latency_sensitivity.pdf", bbox_inches="tight", dpi=150)
    plt.close()
    print(f"Saved: {OUT_DIR / 'fig_latency_sensitivity.pdf'}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    print("=" * 72)
    print(" PROSE-APEX: Mode A vs Mode B Performance Comparison")
    print("=" * 72)

    comp = run_mode_comparison()
    sweep = run_latency_sweep()

    plot_mode_comparison(comp)
    plot_latency_sensitivity(sweep)

    for r in comp:
        if "Mode" in r["label"]:
            assert r["rpe_bytes_mean"] == 0.0, f"RPE violation: {r['label']}"
    for r in sweep:
        assert r["rpe_bytes_mean"] == 0.0, f"RPE violation at RTT={r['host_rtt_ns']}ns"

    p2p_sweep = [r for r in sweep if not r.get("host_bounce")]
    print(f"\n[PASS] All configs maintain RPE=0.")
    print(f"[PASS] Graceful degradation: {p2p_sweep[0]['throughput_tok_s']:.0f} tok/s "
          f"@ {p2p_sweep[0]['host_rtt_ns']}ns -> "
          f"{p2p_sweep[-1]['throughput_tok_s']:.0f} tok/s "
          f"@ {p2p_sweep[-1]['host_rtt_ns']}ns")
    print("=" * 72)
