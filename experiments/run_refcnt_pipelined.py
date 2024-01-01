#!/usr/bin/env python3
"""REFCNT pipelined-atomic sensitivity sweep for P1-3.

Creates a REFCNT variant with zero extra RTT and zero serialized acquire cost
("fully pipelined atomics") and replays the same 32x oversubscription trajectory.
The ordering claim is that even with *zero* synchronization latency, REFCNT still
exhausts the bounded pin pool because it holds one pin per queued candidate, so
its throughput remains bounded by the same Little's-law occupancy.

Outputs:
  experiments/out/refcnt_pipelined/refcnt_pipelined.json
  experiments/out/refcnt_pipelined/refcnt_pipelined_summary.txt
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.oversub_reclaim import (
    OversubConfig, generate_oversub_trace, replay_oversub,
    ProtMech, MECHS,
)

OUT = ROOT / "experiments" / "out" / "refcnt_pipelined"

# REFCNT with fully pipelined atomics: zero RTT, zero serialized acquire cost.
REFCNT_PIPE = ProtMech(
    name="REFCNT_PIPE",
    label="RefCnt pipelined (zero RTT)",
    acquire="enqueue",
    protects_transfer=True,
    extra_rtt=0,
    serialized_acquire_ns=0.0,
)


def run_sweep(oversubs, tenants, seeds, capacity=512, budget=32, n_steps=200):
    rows = []
    for oversub in oversubs:
        for nt in tenants:
            for seed in seeds:
                cfg = OversubConfig(
                    oversubscription=oversub, n_tenants=nt,
                    admit_budget=budget, n_steps=n_steps,
                    bound_mode="capacity", capacity=capacity, seed=seed,
                )
                trace = generate_oversub_trace(cfg)
                r_prose = replay_oversub(trace, MECHS["PROSE"])
                r_ref = replay_oversub(trace, MECHS["REFCNT"])
                r_pipe = replay_oversub(trace, REFCNT_PIPE)
                rows.append({
                    "oversubscription": oversub,
                    "n_tenants": nt,
                    "seed": seed,
                    "prose_tput_Bpns": r_prose["valid_throughput_Bpns"],
                    "refcnt_tput_Bpns": r_ref["valid_throughput_Bpns"],
                    "refcnt_pipe_tput_Bpns": r_pipe["valid_throughput_Bpns"],
                    "refcnt_pipe_p99_ns": r_pipe["admission_p99_ns"],
                    "refcnt_pipe_pinned_peak": r_pipe["pinned_peak"],
                })
    return rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = run_sweep(
        oversubs=[32, 64],
        tenants=[16],
        seeds=list(range(10)),
    )

    # Aggregate over seeds
    agg = {}
    for r in rows:
        k = (r["oversubscription"], r["n_tenants"])
        agg.setdefault(k, []).append(r)

    summary = []
    for k, rs in agg.items():
        prose = mean(r["prose_tput_Bpns"] for r in rs)
        ref = mean(r["refcnt_tput_Bpns"] for r in rs)
        pipe = mean(r["refcnt_pipe_tput_Bpns"] for r in rs)
        p99 = mean(r["refcnt_pipe_p99_ns"] for r in rs)
        pinned = mean(r["refcnt_pipe_pinned_peak"] for r in rs)
        summary.append({
            "oversubscription": k[0],
            "n_tenants": k[1],
            "prose_throughput_Bpns": prose,
            "refcnt_throughput_Bpns": ref,
            "refcnt_pipe_throughput_Bpns": pipe,
            "refcnt_pipe_ratio_to_prose": pipe / prose if prose else 0.0,
            "refcnt_pipe_p99_ns": p99,
            "refcnt_pipe_pinned_peak": pinned,
        })

    json_path = OUT / "refcnt_pipelined.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump({"runs": rows, "summary": summary}, f, indent=2)

    lines = ["REFCNT pipelined-atomic sensitivity (16 tenants, capacity=512)"]
    lines.append(f"{'Oversub':>8} {'PROSE':>10} {'REFCNT':>10} "
                 f"{'REFCNT-PIPE':>12} {'PIPE/PROSE':>12} {'P99 ns':>12} {'Peak pin':>10}")
    for s in summary:
        lines.append(
            f"{s['oversubscription']:>8} {s['prose_throughput_Bpns']:>10.3f} "
            f"{s['refcnt_throughput_Bpns']:>10.3f} {s['refcnt_pipe_throughput_Bpns']:>12.3f} "
            f"{s['refcnt_pipe_ratio_to_prose']:>12.2f} {s['refcnt_pipe_p99_ns']:>12.1f} "
            f"{int(s['refcnt_pipe_pinned_peak']):>10}"
        )
    txt = "\n".join(lines)
    (OUT / "refcnt_pipelined_summary.txt").write_text(txt + "\n")
    print(txt)
    print(f"\nWrote {json_path} and summary.")


if __name__ == "__main__":
    main()
