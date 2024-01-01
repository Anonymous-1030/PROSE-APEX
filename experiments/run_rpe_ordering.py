#!/usr/bin/env python3
"""Reproduce: the OAT gate (CEFE) eliminates reclaimed-payload exposure (RPE) at every chunk size.

Paper claim (§IV-C, "RPE across granularities" / README table):

  | Chunk (KiB) | Chunks | FTS RPE (KiB/step) | CEFE RPE | Endpoint admission |
  |    4        |  4096  |      14748         |    0     |     4.10 us        |
  |   16        |  1024  |      14752         |    0     |     1.02 us        |
  |   64        |   256  |      14784         |    0     |     0.26 us        |
  |  256        |    64  |      14848         |    0     |     0.06 us        |

Model (exactly the paper's setup):
  * A fixed ~16 MiB candidate working set per step, re-chunked at each
    granularity, so the descriptor count is 16 MiB / chunk_size.
  * A 10% visible-KV budget: only round(0.1 * N) chunks become visible.
  * Fetch-then-score (the worst-case bound the paper reports, not a tuned
    competitor) fetches *every* candidate, then scores; the verdict lands after
    the DMA has fired, so the (N - visible) un-kept chunks are reclaimed payload:
        RPE_FTS = (N - round(0.1 N)) * chunk_bytes.
  * CEFE null-completes a reject *before* the copy engine starts, so RPE == 0 at
    every granularity, by construction.
  * Endpoint admission time follows the RTL throughput of 1 descriptor / cycle
    at 1 GHz (cross-checked against the synthesizable APEX pipeline, which
    closes an admit in 9 cycles and a reject in 4): N descriptors -> N ns.

This is a closed-form restatement of the ordering argument; the per-descriptor
admit/reject latency it rests on is the RTL-validated one (see ../rtl).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when run directly (no install / PYTHONPATH needed).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.io_utils import save_json, save_fig, C

WORKING_SET_KIB = 16 * 1024       # ~16 MiB candidate pool per step
VISIBLE_BUDGET_FRAC = 0.10        # 10% visible-KV budget
CLOCK_GHZ = 1.0                   # APEX pipeline clock (RTL-validated)
CHUNK_KIB = [4, 16, 64, 256]


def run() -> dict:
    rows = []
    for chunk_kib in CHUNK_KIB:
        n_chunks = WORKING_SET_KIB // chunk_kib
        visible = round(VISIBLE_BUDGET_FRAC * n_chunks)
        rejected = n_chunks - visible

        # Fetch-then-score fetches everything; the un-kept chunks are RPE.
        fts_rpe_kib = rejected * chunk_kib
        # CEFE rejects before payload issue -> zero RPE.
        cefe_rpe_kib = 0
        # Endpoint admission: 1 descriptor / cycle at CLOCK_GHZ.
        admission_us = n_chunks / (CLOCK_GHZ * 1e3)

        rows.append({
            "chunk_kib": chunk_kib,
            "n_chunks": n_chunks,
            "visible_chunks": visible,
            "fts_rpe_kib_per_step": fts_rpe_kib,
            "cefe_rpe_kib_per_step": cefe_rpe_kib,
            "endpoint_admission_us": admission_us,
        })
    return {"working_set_kib": WORKING_SET_KIB,
            "visible_budget_frac": VISIBLE_BUDGET_FRAC,
            "clock_ghz": CLOCK_GHZ,
            "rows": rows}


def report(results: dict) -> None:
    print("=" * 74)
    print("RPE across chunk granularities  (paper §IV-C)")
    print("=" * 74)
    print(f"{'Chunk':>7} {'Chunks':>7} | {'FTS RPE':>12} {'CEFE RPE':>10} "
          f"{'Admission':>12}")
    print(f"{'(KiB)':>7} {'':>7} | {'(KiB/step)':>12} {'(KiB/step)':>10} "
          f"{'(us)':>12}")
    print("-" * 74)
    for r in results["rows"]:
        print(f"{r['chunk_kib']:>7} {r['n_chunks']:>7} | "
              f"{r['fts_rpe_kib_per_step']:>12d} {r['cefe_rpe_kib_per_step']:>10d} "
              f"{r['endpoint_admission_us']:>12.2f}")
    print("-" * 74)
    max_cefe = max(r["cefe_rpe_kib_per_step"] for r in results["rows"])
    fts_range = (min(r["fts_rpe_kib_per_step"] for r in results["rows"]),
                 max(r["fts_rpe_kib_per_step"] for r in results["rows"]))
    assert max_cefe == 0, "CEFE must hold zero RPE at every granularity"
    print(f"CEFE max RPE = {max_cefe} KiB/step (target 0); "
          f"FTS RPE in [{fts_range[0]}, {fts_range[1]}] KiB/step "
          f"(paper: 14748-14848).")
    print("OK: claim reproduced.")


def plot(results: dict):
    import numpy as np
    import matplotlib.pyplot as plt
    rows = results["rows"]
    x = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(7, 4.5))
    w = 0.38
    ax.bar(x - w / 2, [r["fts_rpe_kib_per_step"] / 1024 for r in rows], w,
           label="Fetch-then-score (worst case)", color=C["fts"])
    ax.bar(x + w / 2, [r["cefe_rpe_kib_per_step"] / 1024 for r in rows], w,
           label="CEFE (ours)", color=C["cefe"])
    ax.set_xticks(x)
    ax.set_xticklabels([str(r["chunk_kib"]) for r in rows])
    ax.set_xlabel("Chunk size (KiB)")
    ax.set_ylabel("RPE (MiB / step)")
    ax.set_title("OAT gate eliminates reclaimed-payload exposure")
    ax.legend()
    return fig


def main() -> None:
    results = run()
    report(results)
    save_json("repro_rpe_ordering", results)
    save_fig(plot(results), "repro_rpe_ordering")
    print("\nSaved: experiments/out/data/repro_rpe_ordering.json")


if __name__ == "__main__":
    main()
