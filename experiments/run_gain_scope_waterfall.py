#!/usr/bin/env python3
"""Separate validation-only gains from the 3.1x end-to-end headline.

The left panel is a same-operating-point cumulative waterfall at the exact
phase-diagram headline regime (2 GB/s, 16 producers, 16x candidate
oversubscription). The right panel deliberately keeps the runtime-observed
validation-only result separate: it is a fixed-load capacity measurement and
must not be added again to the end-to-end waterfall.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from experiments.trace_utils import load_trace, measure_cfo_dedup
from simcxl_ext.cxl_admission_sim import SimConfig, run_closed_loop

OUT_DIR = ROOT / "experiments" / "out" / "gain_scope"
TRACE_PATH = ROOT / "experiments" / "out" / "data" / "trace.jsonl"
RUNTIME_PATH = ROOT / "experiments" / "out" / "runtime_staleness" / "runtime_staleness.json"

N_STEPS = 256
SEED = 0
OP = {
    "cxl_bw_gbs": 2.0,
    "n_hosts": 16,
    "n_candidates": 1024,
    "budget_per_step": 64,
}


def collect() -> dict:
    trace = load_trace(TRACE_PATH, max_steps=400)
    dedup = measure_cfo_dedup(trace)["dedup_frac"]
    specs = [
        ("Fetch-then-score", "fts_none", "none", 0.0),
        ("+ host pre-score", "host_prescore", "odus_x", 0.0),
        ("+ endpoint score / validate", "cefe", "odus_x", 0.0),
        ("+ CFO", "cefe", "odus_x", dedup),
    ]
    stages = []
    for label, boundary, scorer, cfo in specs:
        result = run_closed_loop(
            boundary, scorer, SimConfig(cfo_dedup_frac=cfo, **OP),
            n_steps=N_STEPS, seed=SEED,
        )
        stages.append({
            "label": label,
            "boundary": boundary,
            "scorer": scorer,
            "cfo_dedup_frac": cfo,
            "tok_per_s": result["tok_per_s_mean"],
            "recovery_at_k": result["recovery_at_k_mean"],
            "wasted_bytes_mean": result["wasted_bytes_mean"],
        })

    base = stages[0]["tok_per_s"]
    for stage in stages:
        stage["relative_throughput"] = stage["tok_per_s"] / base

    with RUNTIME_PATH.open("r", encoding="utf-8") as handle:
        runtime = json.load(handle)
    tail = runtime["simcxl_tail_replay"]
    validation_only = tail["endpoint_saturation_load_pct"] / 100.0
    return {
        "operating_point": OP,
        "n_steps": N_STEPS,
        "seed": SEED,
        "measured_cfo_dedup": dedup,
        "stages": stages,
        "validation_only_capacity_gain": validation_only,
        "validation_only_source": str(RUNTIME_PATH),
        "interpretation": {
            "validation_only": (
                "Runtime-observed stale-payload removal at fixed offered load; "
                "not an additive component of the headline waterfall."
            ),
            "headline": (
                "At this 16x candidate-oversubscribed point, pre-transfer "
                "filtering supplies almost all of the 3.13x throughput gain; "
                "endpoint validation supplies atomicity and tail protection."
            ),
        },
    }


def plot(data: dict) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9.5,
        "axes.labelsize": 10, "axes.titlesize": 10.5,
        "xtick.labelsize": 8.4, "ytick.labelsize": 8.6,
        "axes.linewidth": 0.8, "figure.dpi": 180,
        "pdf.fonttype": 42, "ps.fonttype": 42,
    })
    gray, blue, navy, green, red = "#77808A", "#6BA7C9", "#24557A", "#2A7F62", "#C23B32"
    stages = data["stages"]
    values = np.array([stage["relative_throughput"] for stage in stages])
    deltas = np.diff(values)

    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(8.9, 3.8),
                                   gridspec_kw={"width_ratios": [1.45, 1.0]})

    # Cumulative waterfall: base, three incremental mechanisms, total.
    x = np.arange(5)
    bottoms = [0.0, values[0], values[1], values[2], 0.0]
    heights = [values[0], deltas[0], deltas[1], deltas[2], values[-1]]
    colors = [gray, blue, navy, green, green]
    bars = ax0.bar(x, heights, bottom=bottoms, color=colors, width=0.68)
    for index in range(3):
        ax0.plot([x[index] + 0.34, x[index + 1] - 0.34],
                 [values[index], values[index]], color="#A8AFB5", lw=0.8)
    labels = ["FTS", "+ host\npre-score", "+ endpoint\nscore + gate", "+ CFO", "Full\nendpoint"]
    ax0.set_xticks(x); ax0.set_xticklabels(labels)
    ax0.set_ylabel("throughput relative to FTS")
    ax0.set_title("(a) Same-point cumulative throughput decomposition", loc="left", fontweight="bold")
    ax0.set_ylim(0, 3.65)
    ax0.grid(axis="y", color="#D7DBDE", lw=0.6, alpha=0.7)
    ax0.set_axisbelow(True)
    for index, bar in enumerate(bars):
        if index == 0:
            text = f"{values[0]:.2f}x"
        elif index == 4:
            text = f"{values[-1]:.2f}x total"
        else:
            text = f"+{heights[index]:.2f}x"
        y = bottoms[index] + heights[index]
        if index == 1:
            label_y, valign = bottoms[index] + heights[index] * 0.55, "center"
        elif index in (2, 3):
            label_y, valign = 3.34 + (index - 2) * 0.14, "center"
        else:
            label_y, valign = y + 0.05, "bottom"
        ax0.text(bar.get_x() + bar.get_width() / 2, label_y, text,
                 ha="center", va=valign, fontsize=8.3,
                 color=red if index == 1 else "#303438")

    # Scope panel: the runtime validation-only result is intentionally not
    # stacked into the end-to-end waterfall above.
    scope_values = [1.0, data["validation_only_capacity_gain"], values[-1]]
    scope_labels = ["passive\nfixed-load", "validation only\nfixed-load", "full endpoint\n16x candidates"]
    scope_colors = [gray, navy, green]
    scope_bars = ax1.bar(np.arange(3), scope_values, color=scope_colors, width=0.66)
    ax1.set_xticks(np.arange(3)); ax1.set_xticklabels(scope_labels)
    ax1.set_ylabel("relative capacity / throughput")
    ax1.set_title("(b) Keep validation-only and headline scopes separate", loc="left", fontweight="bold")
    ax1.set_ylim(0, 3.65)
    ax1.grid(axis="y", color="#D7DBDE", lw=0.6, alpha=0.7)
    ax1.set_axisbelow(True)
    for bar, value in zip(scope_bars, scope_values):
        ax1.text(bar.get_x() + bar.get_width() / 2, value + 0.05,
                 f"{value:.2f}x", ha="center", va="bottom", fontsize=8.5)

    fig.suptitle(
        "Scope of the 3.1x claim: validation protects atomicity;\n"
        "pre-transfer filtering drives throughput",
        fontsize=11.5, fontweight="bold", y=0.99,
    )
    fig.text(
        0.06, 0.02,
        "Runtime validation-only capacity comes from the measured stale-event replay. "
        "Waterfall stages are SimCXL results at one identical point "
        "(2 GB/s, 16 producers, 16x candidates).\n"
        "CFO adds no throughput here because the endpoint has already reached the compute ceiling.",
        fontsize=7.2, color="#555B60",
    )
    fig.subplots_adjust(left=0.075, right=0.98, bottom=0.22, top=0.84, wspace=0.30)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUT_DIR / "fig_gain_scope_waterfall.pdf"
    fig.savefig(output, bbox_inches="tight")
    fig.savefig(output.with_suffix(".png"), bbox_inches="tight", dpi=240)
    plt.close(fig)
    return output


def main() -> int:
    data = collect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "gain_scope_waterfall.json").open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    figure = plot(data)
    values = [stage["relative_throughput"] for stage in data["stages"]]
    assert values[-1] >= values[1]
    assert 1.0 < data["validation_only_capacity_gain"] < values[-1]
    print(f"validation-only capacity: {data['validation_only_capacity_gain']:.3f}x")
    print(f"full endpoint headline point: {values[-1]:.3f}x")
    print(f"Saved: {figure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
