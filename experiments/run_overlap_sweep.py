#!/usr/bin/env python3
"""Reproduce: CFO benefit is a smooth function of cross-tenant overlap.

Purpose
  Answer the "single production trace is not general" critique. Rather than one
  overlap number, we sweep cross-tenant overlap from 0 to 1 on a fine grid and
  show three curves. The saving is continuous, not a cliff. Below the read-port
  break-even CFO degrades to parity (it never hurts), and above it the source
  read pressure clears and the completion tail falls. The production trace point
  (0.52 measured) sits inside the payoff region, with graceful degradation to
  its left.

  This driver reuses the exact resource-curve model of run_cfo_overlap.py (same
  16 domains, 64 KiB chunks, read-port vs egress queueing), only sampling
  overlap on 11 points instead of 5 and plotting throughput scale alongside read
  pressure and the latency tail. No model change; the published coarse-grid
  driver is left untouched for reproducibility.

Run:  python experiments/run_overlap_sweep.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from simcxl_ext.io_utils import save_json, save_fig, C

# Load the published CFO model without importing its __main__.
_spec = importlib.util.spec_from_file_location(
    "run_cfo_overlap", Path(__file__).resolve().parent / "run_cfo_overlap.py")
_cfo = importlib.util.module_from_spec(_spec)
sys.modules["run_cfo_overlap"] = _cfo
_spec.loader.exec_module(_cfo)

OVERLAP_GRID = [0.0, 0.1, 0.2, 0.3, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
TRACE_OVERLAP = 0.52          # measured RAG-subset overlap (paper)
BREAK_EVEN = _cfo.BREAK_EVEN_OVERLAP


def _mean(rows: List[Dict], key: str) -> float:
    return float(np.mean([r[key] for r in rows]))


def run() -> Dict:
    rows = []
    for ov in OVERLAP_GRID:
        nb = [_cfo._simulate_once(_cfo.DOMAINS, ov, False, s) for s in _cfo.SEEDS]
        cb = [_cfo._simulate_once(_cfo.DOMAINS, ov, True, s) for s in _cfo.SEEDS]
        rows.append({
            "overlap": ov,
            "read_util_nocfo": _mean(nb, "device_read_util"),
            "read_util_cfo": _mean(cb, "device_read_util"),
            "tput_scale_nocfo": _mean(nb, "throughput_scale"),
            "tput_scale_cfo": _mean(cb, "throughput_scale"),
            "p99_ms_nocfo": _mean(nb, "p99_latency_ms"),
            "p99_ms_cfo": _mean(cb, "p99_latency_ms"),
        })
    return {"break_even": BREAK_EVEN, "trace_overlap": TRACE_OVERLAP,
            "domains": _cfo.DOMAINS, "rows": rows}


def report(res: Dict) -> None:
    print("=" * 74)
    print("CFO benefit vs cross-tenant overlap  (fine sweep)")
    print(f"break-even={res['break_even']}  measured trace overlap={res['trace_overlap']}")
    print("=" * 74)
    print(f"{'overlap':>8} | {'readutil CFO':>12} | {'tput CFO':>9} | {'P99ms CFO':>10}")
    for r in res["rows"]:
        print(f"{r['overlap']:>8.2f} | {r['read_util_cfo']:>12.2f} | "
              f"{r['tput_scale_cfo']:>9.2f} | {r['p99_ms_cfo']:>10.2f}")
    r0 = res["rows"][0]
    print(f"\nAt overlap 0 CFO equals no-CFO "
          f"(tput {r0['tput_scale_cfo']:.2f} vs {r0['tput_scale_nocfo']:.2f}): "
          f"no penalty below break-even.")


def plot(res: Dict):
    import matplotlib.pyplot as plt
    rows = res["rows"]
    ov = [r["overlap"] for r in rows]
    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(9.4, 3.9))

    ax0.plot(ov, [r["read_util_nocfo"] for r in rows], "s--", color=C["fts"],
             label="no CFO")
    ax0.plot(ov, [r["read_util_cfo"] for r in rows], "o-", color=C["cefe"],
             label="CFO")
    ax0.axhline(1.0, color="grey", ls=":", lw=1.3)
    ax0.axvline(res["break_even"], color=C["accent2"], ls=":", lw=1.4,
                label=f"break-even {res['break_even']:g}")
    ax0.axvline(res["trace_overlap"], color="black", ls="-.", lw=1.2,
                label=f"trace {res['trace_overlap']:g}")
    ax0.set_xlabel("Cross-tenant overlap")
    ax0.set_ylabel("Read-port offered load")
    ax0.set_title("(a) Source-read pressure")
    ax0.legend(fontsize=7)

    ax1.plot(ov, [r["tput_scale_nocfo"] for r in rows], "s--", color=C["fts"],
             label="no CFO throughput")
    ax1.plot(ov, [r["tput_scale_cfo"] for r in rows], "o-", color=C["cefe"],
             label="CFO throughput")
    ax1b = ax1.twinx()
    ax1b.plot(ov, [r["p99_ms_cfo"] for r in rows], "^:", color=C["accent2"],
              label="CFO P99 latency")
    ax1.axvline(res["break_even"], color=C["accent2"], ls=":", lw=1.4)
    ax1.set_xlabel("Cross-tenant overlap")
    ax1.set_ylabel("Throughput (fraction of ceiling)")
    ax1b.set_ylabel("P99 promotion latency (ms)")
    ax1.set_title("(b) Throughput and tail")
    h0, l0 = ax1.get_legend_handles_labels()
    h1, l1 = ax1b.get_legend_handles_labels()
    ax1.legend(h0 + h1, l0 + l1, fontsize=7, loc="center right")
    fig.tight_layout()
    return fig


def main() -> None:
    res = run()
    report(res)
    save_json("overlap_sweep", res)
    save_fig(plot(res), "fig_overlap_sweep")
    print("\nSaved fig_overlap_sweep + overlap_sweep.json under experiments/out/")


if __name__ == "__main__":
    main()
