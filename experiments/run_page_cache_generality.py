#!/usr/bin/env python3
"""Experiment B driver — page/block-cache generality of the object contract.

Design analysis + calibrated projection (NOT a KV measurement). Prints the
design-mapping table, then re-parameterizes the mechanistic binding model with
page-cache access characteristics and reports, per replacement policy:

  * unmitigated RPE (stale prefetch moves a reused frame) — shows the four
    exposure conditions of §II-B hold for a page cache;
  * RPE == 0 under the OAT gate;
  * protection-span ratio vs. a refcount (transfer-scoped 1.0x vs. queue-wide);
  * bandwidth efficiency (useful / issued).

Outputs (experiments/out/page_cache/):
  * page_cache_mapping.txt        — the design-mapping table
  * page_cache_generality.json    — per-policy numbers
  * page_cache_generality.txt     — human-readable summary
  * fig_page_cache_generality.{pdf,png}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.page_cache_instance import (   # noqa: E402
    PageConfig, print_mapping, measure_page_rpe, protection_duration_ratio,
    DESIGN_MAPPING,
)

OUT = ROOT / "experiments" / "out" / "page_cache"
POLICIES = ["LRU", "ARC", "SIEVE", "FIFO"]


def run() -> Dict:
    results: Dict = {"policies": {}, "protection_duration": None, "mapping": DESIGN_MAPPING}
    for pol in POLICIES:
        cfg = PageConfig(policy=pol)
        unmit = measure_page_rpe(cfg, gated=False)
        gated = measure_page_rpe(cfg, gated=True)
        results["policies"][pol] = {"unmitigated": unmit, "gated": gated}
    results["protection_duration"] = protection_duration_ratio(PageConfig())
    return results


def make_figure(results: Dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pols = POLICIES
    unmit_rpe = [results["policies"][p]["unmitigated"]["rpe_payload_frac"] * 100 for p in pols]
    gated_rpe = [results["policies"][p]["gated"]["rpe_payload_frac"] * 100 for p in pols]
    unmit_bw = [results["policies"][p]["unmitigated"]["bandwidth_efficiency"] * 100 for p in pols]
    gated_bw = [results["policies"][p]["gated"]["bandwidth_efficiency"] * 100 for p in pols]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    import numpy as np
    x = np.arange(len(pols)); w = 0.38

    ax = axes[0]
    ax.bar(x - w/2, unmit_rpe, w, label="Unmitigated", color="#d95f02")
    ax.bar(x + w/2, gated_rpe, w, label="OAT-gated (PROSE)", color="#1b9e77")
    ax.set_xticks(x); ax.set_xticklabels(pols)
    ax.set_ylabel("RPE payload (% of issued bytes)")
    ax.set_title("(a) Page-cache RPE: unmitigated vs. OAT-gated")
    ax.legend(fontsize=9); ax.grid(alpha=0.3, axis="y")
    for xi, v in zip(x + w/2, gated_rpe):
        ax.text(xi, 0.3, "0", ha="center", va="bottom", fontsize=8, color="#1b9e77")

    ax = axes[1]
    pd = results["protection_duration"]
    ax.bar(["PROSE\n(transfer-scoped)", "RefCnt\n(before enqueue)"],
           [pd["prose_span_ratio"], pd["refcnt_span_ratio"]],
           color=["#1b9e77", "#d95f02"], width=0.55)
    ax.set_ylabel("Protection span / transfer span")
    ax.set_title("(b) Protection duration (page-cache instance)")
    ax.grid(alpha=0.3, axis="y")
    ax.text(0, pd["prose_span_ratio"] + 0.5, f"{pd['prose_span_ratio']:.1f}x",
            ha="center", fontsize=10)
    ax.text(1, pd["refcnt_span_ratio"] + 0.5, f"{pd['refcnt_span_ratio']:.0f}x",
            ha="center", fontsize=10)

    fig.suptitle("Contract generality: endpoint-managed page/block cache "
                 "(design analysis + calibrated projection)", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(OUT / f"fig_page_cache_generality.{ext}", dpi=150, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mapping = print_mapping()
    (OUT / "page_cache_mapping.txt").write_text(mapping + "\n")
    print(mapping)
    print()

    results = run()
    (OUT / "page_cache_generality.json").write_text(json.dumps(results, indent=2, default=str))

    lines = ["Page/Block-cache instance — RPE and protection (calibrated projection)",
             f"(4 KiB pages, {PageConfig().n_frames} frames, "
             f"{PageConfig().working_pages}-page working set, prefetch+random)", ""]
    lines.append(f"{'policy':>7} {'unmit_RPE%':>11} {'gated_RPE%':>11} "
                 f"{'unmit_BWeff%':>13} {'gated_BWeff%':>13} {'rejected':>9}")
    for p in POLICIES:
        u = results["policies"][p]["unmitigated"]
        g = results["policies"][p]["gated"]
        lines.append(f"{p:>7} {u['rpe_payload_frac']*100:>10.1f}% "
                     f"{g['rpe_payload_frac']*100:>10.1f}% "
                     f"{u['bandwidth_efficiency']*100:>12.1f}% "
                     f"{g['bandwidth_efficiency']*100:>12.1f}% "
                     f"{g['rejected_reads']:>9}")
    pd = results["protection_duration"]
    lines.append("")
    lines.append(f"Protection span: PROSE {pd['prose_span_ratio']:.2f}x transfer, "
                 f"RefCnt {pd['refcnt_span_ratio']:.1f}x transfer "
                 f"(transfer {pd['transfer_ns']:.0f} ns, queue residence "
                 f"{pd['residence_ns']:.0f} ns).")
    txt = "\n".join(lines)
    (OUT / "page_cache_generality.txt").write_text(txt + "\n")
    print(txt)

    make_figure(results)
    print(f"\nOutputs in {OUT}")


if __name__ == "__main__":
    main()
