#!/usr/bin/env python3
"""Endpoint-model sensitivity beyond SimCXL — upgraded enclosure figure.

Produces a single publication figure (IEEE single-column, 3.45 in width) with
two side-by-side tornado panels:
  Left:  P99 admission latency sensitivity (ns)
  Right: Throughput speedup vs FTS sensitivity

Plus a combined worst-case corner reported in the console output (for a table).

Parameters swept (all PROSE-added, not inherited SimCXL core):
  - Tenant count:           1, 2, 4, 8, 16, 32
  - DMA/arb stage delay:    0.25x, 0.5x, 1x, 2x, 4x, 8x
  - ABA switch threshold:   0.50, 0.65, 0.75, 0.85, 0.95
  - P2P write latency:      +0, +100, +200, +500, +1000 ns
  - CXL bandwidth:          2, 4, 8, 16, 32, 64 GB/s
  - CFO source overlap:     0%, 10%, 25%, 35%, 50%

The 1-us red line is the per-step admission budget: at K=25 descriptors/step
and 1 ms decode step, each descriptor gets at most 1000/25 = 40 us; 1 us is a
conservative bound ensuring admission stays < 2.5% of the step budget even at
P99 with 16 hosts submitting K=25 each (16*25*1us = 400 us = 40% of step).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.endpoint_sim import (
    EndpointConfig, EndpointState, DescriptorBurst, simulate_endpoint_burst,
)
from simcxl_ext.cxl_admission_sim import SimConfig, run_closed_loop
from simcxl_ext.io_utils import save_json, C, FIG_DIR

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

SEED = 42


# ============================================================================
# Core measurement functions
# ============================================================================

def measure_p99(n_hosts, cfg, descs_per_host=64, n_trials=8):
    all_lat = []
    cycle_ns = 1000.0 / cfg.scorer_clock_mhz
    for trial in range(n_trials):
        state = EndpointState()
        rng = np.random.default_rng(SEED + trial * 17 + n_hosts)
        for host in range(n_hosts):
            inter = cfg.fast_path_cycles * cycle_ns / max(1, n_hosts // 2)
            burst = DescriptorBurst(
                n_descriptors=descs_per_host,
                inter_arrival_ns=inter,
                tenant_id=host,
            )
            accept = max(0.25, 0.70 - 0.03 * n_hosts)
            state = simulate_endpoint_burst(
                burst, cfg, state, scorer_accept_rate=accept, rng=rng,
            )
        all_lat.extend(state.latencies)
    lat = np.array(all_lat)
    return float(np.percentile(lat, 99))


def measure_speedup(sim_cfg):
    out = run_closed_loop("cefe", "odus_x", sim_cfg, n_steps=128, seed=SEED)
    fts = run_closed_loop("fts_none", "none", sim_cfg, n_steps=128, seed=SEED)
    return out["tok_per_s_mean"] / max(1e-9, fts["tok_per_s_mean"])


# ============================================================================
# Sweeps
# ============================================================================

def run_all():
    data = {}

    # --- Latency-affecting parameters ---
    print("  [1/6] Tenant count (1-32)...")
    vals = [1, 2, 4, 8, 16, 32]
    data["tenants"] = {
        "label": "Tenants (1-32)",
        "baseline_x": 8,
        "points": [{"x": n, "p99": measure_p99(n, EndpointConfig())} for n in vals],
    }

    print("  [2/6] DMA/arb delay (0.25x-8x)...")
    mults = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]
    pts = []
    for m in mults:
        cfg = EndpointConfig()
        cfg.dma_initiation_cycles = max(1, int(4 * m))
        pts.append({"x": m, "p99": measure_p99(8, cfg)})
    data["dma_arb"] = {"label": "DMA/arb (0.25x-8x)", "baseline_x": 1.0, "points": pts}

    print("  [3/6] ABA switch threshold (0.50-0.95)...")
    bps = [0.50, 0.65, 0.75, 0.85, 0.95]
    pts = []
    for bp in bps:
        cfg = EndpointConfig()
        cfg.backpressure_threshold = bp
        pts.append({"x": bp, "p99": measure_p99(8, cfg)})
    data["aba_threshold"] = {"label": "ABA threshold (0.50-0.95)", "baseline_x": 0.85, "points": pts}

    print("  [4/6] P2P write latency (+0 to +1000 ns)...")
    offsets = [0, 100, 200, 500, 1000]
    pts = []
    for off in offsets:
        # P2P latency affects promotion-completion time, not admission verdict.
        # Model it as reduced decode-slack (less overlap with compute).
        extra_us = off * 25 / 1000.0  # K=25 chunks * offset_ns / 1000
        sim_cfg = SimConfig(decode_slack_us=8000.0 - extra_us)
        r = measure_speedup(sim_cfg)
        pts.append({"x": off, "speedup": r})
    data["p2p_latency"] = {"label": "P2P write (+0-1 us)", "baseline_x": 0, "points": pts}

    # --- Throughput/speedup-affecting parameters ---
    print("  [5/6] CXL bandwidth (2-64 GB/s)...")
    bws = [2, 4, 8, 16, 32, 64]
    data["cxl_bw"] = {
        "label": "CXL BW (2-64 GB/s)",
        "baseline_x": 32,
        "points": [{"x": bw, "speedup": measure_speedup(SimConfig(cxl_bw_gbs=float(bw)))} for bw in bws],
    }

    print("  [6/6] Candidate pressure (256-4096)...")
    cands = [256, 512, 1024, 2048, 4096]
    data["candidates"] = {
        "label": "Descriptors (256-4096)",
        "baseline_x": 1024,
        "points": [{"x": nc, "speedup": measure_speedup(SimConfig(n_candidates=nc))} for nc in cands],
    }

    # CFO overlap: at 0% overlap CFO adds no coalescing value, speedup comes
    # only from pre-payload rejection. Model overlap as useful_fraction boost.
    print("  [7/7] CFO overlap (0-50%)...")
    overlaps = [0.0, 0.10, 0.25, 0.35, 0.50]
    cfo_pts = []
    for ov in overlaps:
        # Higher overlap -> more shared reads coalesced -> higher effective BW
        # Model: at overlap ov, effective BW = base_bw / (1 - 0.35*ov) (source read saving)
        eff_bw = 32.0 / (1.0 - 0.354 * ov)
        r = measure_speedup(SimConfig(cxl_bw_gbs=eff_bw))
        cfo_pts.append({"x": ov, "speedup": r})
    data["cfo_overlap"] = {
        "label": "CFO overlap (0-50%)",
        "baseline_x": 0.35,
        "points": cfo_pts,
    }

    # --- Combined worst-case corner ---
    # For latency: worst direction = more hosts + higher DMA + HIGHER BP threshold
    # (higher threshold = backpressure triggers later = more queueing = higher tail)
    print("  [*] Combined pessimistic corner...")
    worst_cfg = EndpointConfig()
    worst_cfg.dma_initiation_cycles = 32  # 8x baseline
    worst_cfg.backpressure_threshold = 0.95  # late backpressure = more queueing
    combined_p99 = measure_p99(32, worst_cfg)  # 32 hosts

    # For throughput: worst direction = low BW + high P2P offset
    worst_sim = SimConfig(cxl_bw_gbs=2.0, n_candidates=4096,
                          decode_slack_us=8000.0 - 25.0)  # +1us P2P
    combined_speedup = measure_speedup(worst_sim)
    data["combined_worst"] = {
        "p99_ns": combined_p99,
        "speedup_vs_fts": combined_speedup,
        "description": "Latency: 32 hosts, DMA 8x, BP 0.95 | Throughput: BW 2GB/s, 4096 cands, P2P +1us",
    }

    return data


# ============================================================================
# Plot: single figure, two tornado panels side by side
# ============================================================================

def plot(data):
    fig, (ax_l, ax_r) = plt.subplots(1, 2, figsize=(3.45, 1.9),
                                      gridspec_kw={"width_ratios": [1.1, 1.0]})
    fig.subplots_adjust(left=0.005, right=0.99, top=0.72, bottom=0.20, wspace=1.1)

    bar_h = 0.4

    # --- Left panel: P99 Latency ---
    lat_keys = ["tenants", "dma_arb", "aba_threshold"]
    lat_entries = []
    for key in lat_keys:
        d = data[key]
        p99s = [pt["p99"] for pt in d["points"]]
        base = next(pt["p99"] for pt in d["points"] if pt["x"] == d["baseline_x"])
        lat_entries.append((d["label"], min(p99s), base, max(p99s)))

    for i, (lbl, mn, base, mx) in enumerate(lat_entries):
        ax_l.barh(i, mx - mn, left=mn, height=bar_h,
                  color="#2ca02c", alpha=0.22, edgecolor="#2ca02c", linewidth=0.8)
        ax_l.plot(base, i, "D", color="#1f77b4", ms=4.5, zorder=5)
        ax_l.plot(mn, i, "|", color="#2ca02c", ms=8, mew=1.3, zorder=5)
        ax_l.plot(mx, i, "|", color="#d62728", ms=8, mew=1.3, zorder=5)

    # Pessimistic corner: star marker (joint perturbation, not OAT)
    cw = data["combined_worst"]["p99_ns"]
    ax_l.plot(cw, len(lat_entries) - 0.5, "*", color="#9467bd", ms=10, zorder=6,
              markeredgecolor="k", markeredgewidth=0.3)
    ax_l.annotate("pessimistic corner", xy=(cw, len(lat_entries) - 0.5),
                  xytext=(cw + 110, len(lat_entries) + 0.05),
                  fontsize=5, color="#9467bd", va="bottom", ha="left",
                  arrowprops=dict(arrowstyle="->", color="#9467bd", lw=0.6))

    # 1-us admission budget line
    ax_l.axvline(1000, color="#d62728", ls="--", lw=1.2, alpha=0.9)
    ax_l.text(1010, 1.0, "1 $\mu$s budget", fontsize=5.5,
              color="#d62728", ha="left", va="center")

    ax_l.set_yticks(range(len(lat_entries)))
    ax_l.set_yticklabels([e[0] for e in lat_entries], fontsize=5.5,
                         rotation=30, ha="right", rotation_mode="anchor")
    ax_l.set_xlabel("P99 Latency (ns)", fontsize=6.5)
    ax_l.set_xlim(0, 1150)
    ax_l.set_ylim(-0.9, len(lat_entries) - 0.3)
    ax_l.tick_params(axis="x", labelsize=5.5)
    ax_l.tick_params(axis="y", pad=0)
    ax_l.spines["top"].set_visible(False)
    ax_l.spines["right"].set_visible(False)
    ax_l.set_title("(a) Timing sensitivity", fontsize=7, fontweight="bold", pad=18)

    # --- Right panel: Speedup vs FTS ---
    spd_keys = ["cxl_bw", "cfo_overlap", "p2p_latency", "candidates"]
    spd_entries = []
    for key in spd_keys:
        d = data[key]
        spds = [pt["speedup"] for pt in d["points"]]
        base = next(pt["speedup"] for pt in d["points"] if pt["x"] == d["baseline_x"])
        spd_entries.append((d["label"], min(spds), base, max(spds)))

    for i, (lbl, mn, base, mx) in enumerate(spd_entries):
        ax_r.barh(i, mx - mn, left=mn, height=bar_h,
                  color="#1f77b4", alpha=0.22, edgecolor="#1f77b4", linewidth=0.8)
        ax_r.plot(base, i, "D", color="#1f77b4", ms=4.5, zorder=5)
        ax_r.plot(mn, i, "|", color="#d62728", ms=8, mew=1.3, zorder=5)
        ax_r.plot(mx, i, "|", color="#2ca02c", ms=8, mew=1.3, zorder=5)

    # Pessimistic corner: star marker
    cw_spd = data["combined_worst"]["speedup_vs_fts"]
    ax_r.plot(cw_spd, len(spd_entries) - 0.5, "*", color="#9467bd", ms=10, zorder=6,
              markeredgecolor="k", markeredgewidth=0.3)
    ax_r.annotate("pessimistic corner", xy=(cw_spd, len(spd_entries) - 0.5),
                  xytext=(cw_spd - 1.0, len(spd_entries) + 0.05),
                  fontsize=5, color="#9467bd", va="bottom", ha="right",
                  arrowprops=dict(arrowstyle="->", color="#9467bd", lw=0.6))

    # FTS parity line
    ax_r.axvline(1.0, color="#d62728", ls="--", lw=1.2, alpha=0.9)
    ax_r.text(1.05, len(spd_entries) * 0.5, "FTS parity", fontsize=5.5,
              color="#d62728", ha="left", va="center")

    ax_r.set_yticks(range(len(spd_entries)))
    ax_r.set_yticklabels([e[0] for e in spd_entries], fontsize=5.5,
                         rotation=30, ha="right", rotation_mode="anchor")
    ax_r.set_xlabel("Speedup vs FTS (x)", fontsize=6.5)
    ax_r.set_ylim(-0.9, len(spd_entries) - 0.3)
    ax_r.tick_params(axis="x", labelsize=5.5)
    ax_r.tick_params(axis="y", pad=0)
    ax_r.spines["top"].set_visible(False)
    ax_r.spines["right"].set_visible(False)
    ax_r.set_title("(b) Throughput sensitivity", fontsize=7, fontweight="bold", pad=18)

    # Shared legend
    legend_elements = [
        Line2D([0], [0], marker="D", color="w", markerfacecolor="#1f77b4",
               ms=4.5, label="Baseline"),
        Line2D([0], [0], marker="|", color="#2ca02c", ms=8, mew=1.3,
               linestyle="None", label="Best OAT"),
        Line2D([0], [0], marker="|", color="#d62728", ms=8, mew=1.3,
               linestyle="None", label="Worst OAT"),
        Line2D([0], [0], marker="*", color="#9467bd", ms=8,
               linestyle="None", label="Pessimistic corner"),
    ]
    fig.legend(handles=legend_elements, fontsize=5.5, loc="upper center",
               ncol=4, frameon=False, bbox_to_anchor=(0.5, 1.0))

    fig.suptitle("Sensitivity of PROSE-Added Endpoint Model Parameters",
                 fontsize=7.5, fontweight="bold", y=1.06)
    return fig


# ============================================================================
# Main
# ============================================================================

def main():
    print("=" * 64)
    print("Endpoint-Model Sensitivity Beyond SimCXL")
    print("=" * 64)

    data = run_all()

    # Console report
    print("\n--- Per-parameter P99 ranges ---")
    for key in ["tenants", "dma_arb", "aba_threshold"]:
        d = data[key]
        p99s = [pt["p99"] for pt in d["points"]]
        base = next(pt["p99"] for pt in d["points"] if pt["x"] == d["baseline_x"])
        print(f"  {d['label']:40s}  min={min(p99s):6.1f}  base={base:6.1f}  max={max(p99s):6.1f} ns")

    print("\n--- Per-parameter speedup ranges ---")
    for key in ["cxl_bw", "candidates", "p2p_latency"]:
        d = data[key]
        spds = [pt["speedup"] for pt in d["points"]]
        base = next(pt["speedup"] for pt in d["points"] if pt["x"] == d["baseline_x"])
        print(f"  {d['label']:40s}  min={min(spds):5.2f}x  base={base:5.2f}x  max={max(spds):5.2f}x")

    cw = data["combined_worst"]
    print(f"\n--- Combined pessimistic corner ---")
    print(f"  Config: {cw['description']}")
    print(f"  P99 admission latency: {cw['p99_ns']:.1f} ns")
    print(f"  Speedup vs FTS:        {cw['speedup_vs_fts']:.2f}x")
    print(f"  Below 1 us budget:     {'YES' if cw['p99_ns'] < 1000 else 'NO'}")
    print(f"  Above FTS parity:      {'YES' if cw['speedup_vs_fts'] > 1.0 else 'NO'}")

    fig = plot(data)
    save_json("sensitivity_enclosure", data)
    # Save with constrained bbox (not tight) to preserve exact 3.45-in width
    from simcxl_ext.io_utils import FIG_DIR
    fig.savefig(FIG_DIR / "sensitivity_enclosure.pdf", bbox_inches="tight",
                pad_inches=0.02)
    fig.savefig(FIG_DIR / "sensitivity_enclosure.png", bbox_inches="tight",
                pad_inches=0.02, dpi=300)
    plt.close(fig)
    print("\nOutput: experiments/out/figures/sensitivity_enclosure.pdf")


if __name__ == "__main__":
    main()
