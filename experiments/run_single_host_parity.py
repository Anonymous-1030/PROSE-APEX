#!/usr/bin/env python3
"""Single-host parity check: endpoint admission vs host/GPU software pre-score.

The paper claims in three places (Sec. on placement isolation, scorer table,
and contract discussion) that on a SINGLE host, endpoint admission and
host/GPU software pre-scoring are within ~1% of each other, quoted as
"128 vs. 130 tok/s". Those numbers had no generator in the repo. This driver
produces the real numbers from the existing closed-form machinery
(simcxl_ext.cxl_admission_sim), reusing the exact functions and the exact
operating point of experiments/run_host_prescore.py:

  * per-step tok/s for both paths comes from simulate_step() via
    run_closed_loop() — the same functions that produce the host_prescore
    curves;
  * the operating point is run_host_prescore.STARVED (4 GB/s default,
    decode_compute_us=2000, decode_slack_us=400) with n_hosts forced to 1;
  * oversubscription is 1x (n_candidates == budget == 64): no over-fetch, so
    both paths admit byte-identical sets — the comparison isolates PLACEMENT
    (where the scorer runs), which is exactly what the paper's claim is about.

Apples-to-apples guarantees (asserted, not assumed):
  * same scorer policy on both paths ("odus_x" — the paper scorer);
  * same candidate sets: run_closed_loop's RNG stream is boundary-independent,
    so equal seeds replay identical synthesized steps on both paths;
  * same admitted payload and metadata bytes per step (asserted per seed);
  * cfo_dedup_frac = 0 on both paths (CFO is a cross-tenant mechanism; with
    one host there is nothing to coalesce, and run_host_prescore also gives
    the host path dedup=0).

Only the placement differs:
  * "cefe"          : on-endpoint admission (Mode A), 3.9 us/cand decision,
                      no compute contention, 256 MetaRead credits;
  * "host_prescore" : host reads the same 64 B metadata, runs the same odus_x
                      scorer, endpoint enforces the verdict — 6.0 us/cand,
                      contends with compute, 64 credits
                      (the deliberately FAIR strong baseline from
                      experiments/run_host_prescore.py);
  * "sw_gpu" is recorded as a secondary variant because the paper's wording
    is "host/GPU software pre-scoring" (persistent-kernel PCM, 5.2 us/cand).

No shim was needed: SimConfig already expresses n_hosts=1 and
n_candidates == budget (1x oversubscription) directly; n_hosts does not enter
the cefe/host_prescore throughput path at all (it only affects two_phase and
cefe_passive), so the 1-host/1x configuration is clean.

Outputs:
  results/single_host_parity.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import math
import numpy as np

from simcxl_ext.cxl_admission_sim import SimConfig, run_closed_loop
from experiments.run_host_prescore import BUDGET, N_STEPS, STARVED

OUT_PATH = Path(__file__).resolve().parent.parent / "results" / \
    "single_host_parity.json"

# 1x oversubscription: candidates == budget (no over-fetch, no contention).
OVERSUB = 1.0
N_HOSTS = 1
# Primary bandwidth point is the host_prescore script's starved point (4 GB/s);
# the model is bandwidth-sensitive (transport dominates the step), so bracket
# it with 2 and 8 GB/s as well.
BANDWIDTHS_GBS = [2.0, 4.0, 8.0]
PRIMARY_BW_GBS = 4.0
SEEDS = [42, 123, 2024, 7, 9001]

# (label, boundary, scorer) — same scorer policy on every path.
ENDPOINT = ("endpoint", "cefe", "odus_x")
HOST_SW = ("host_sw_prescore", "host_prescore", "odus_x")
SECONDARY = [("host_sw_gpu", "sw_gpu", "odus_x")]

# Student-t critical values (two-sided 95%) for small sample sizes.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}


def mean_ci95(xs) -> tuple[float, float]:
    xs = np.asarray(xs, dtype=float)
    m = float(np.mean(xs))
    n = len(xs)
    if n < 2:
        return m, 0.0
    sd = float(np.std(xs, ddof=1))
    t = _T95.get(n - 1, 1.96)
    return m, t * sd / math.sqrt(n)


def run_pair(bw_gbs: float, seed: int) -> dict:
    """One seed at one bandwidth: endpoint vs host-sw pre-score, plus the
    secondary sw_gpu variant. Returns per-boundary closed-loop results."""
    cfg = SimConfig(
        n_candidates=int(BUDGET * OVERSUB),
        budget_per_step=BUDGET,
        cxl_bw_gbs=bw_gbs,
        decode_compute_us=STARVED["decode_compute_us"],
        decode_slack_us=STARVED["decode_slack_us"],
        top_k_useful=STARVED["top_k_useful"],
        useful_fraction=STARVED["useful_fraction"],
        n_hosts=N_HOSTS,
        cfo_dedup_frac=0.0,  # single host: no cross-tenant dedup on EITHER path
    )
    out = {}
    for label, boundary, scorer in [ENDPOINT, HOST_SW] + SECONDARY:
        out[label] = run_closed_loop(boundary, scorer, cfg,
                                     n_steps=N_STEPS, seed=seed)
    return out


def gap(endpoint_mean: float, host_mean: float) -> tuple[float, str]:
    """Signed gap of endpoint vs host-sw, in % of the host-sw number."""
    g = (endpoint_mean - host_mean) / host_mean * 100.0
    if abs(g) < 1e-9:
        direction = "tie"
    elif g > 0:
        direction = "endpoint_higher"
    else:
        direction = "host_sw_higher"
    return g, direction


def main():
    print("=" * 78)
    print(" Single-host parity: endpoint admission vs host/GPU SW pre-score")
    print(f" oversub={OVERSUB}x  hosts={N_HOSTS}  budget={BUDGET}  "
          f"steps={N_STEPS}  seeds={SEEDS}")
    print("=" * 78)

    per_bw = []
    for bw in BANDWIDTHS_GBS:
        seed_runs = {s: run_pair(bw, s) for s in SEEDS}

        # ---- Apples-to-apples assertions: identical bytes on both paths.
        for s in SEEDS:
            ep, hs = seed_runs[s][ENDPOINT[0]], seed_runs[s][HOST_SW[0]]
            assert ep["committed_bytes_mean"] == hs["committed_bytes_mean"], \
                f"seed {s}: admitted payload differs — not placement-only"
            assert ep["meta_bytes_mean"] == hs["meta_bytes_mean"], \
                f"seed {s}: metadata bytes differ — not placement-only"

        ep_toks = [seed_runs[s][ENDPOINT[0]]["tok_per_s_mean"] for s in SEEDS]
        hs_toks = [seed_runs[s][HOST_SW[0]]["tok_per_s_mean"] for s in SEEDS]
        ep_m, ep_ci = mean_ci95(ep_toks)
        hs_m, hs_ci = mean_ci95(hs_toks)
        g, direction = gap(ep_m, hs_m)

        row = {
            "cxl_bw_gbs": bw,
            "endpoint_toks_mean": ep_m, "endpoint_toks_ci95": ep_ci,
            "host_sw_toks_mean": hs_m, "host_sw_toks_ci95": hs_ci,
            "gap_pct": g, "direction": direction,
            "endpoint_toks_per_seed": dict(zip(map(str, SEEDS), ep_toks)),
            "host_sw_toks_per_seed": dict(zip(map(str, SEEDS), hs_toks)),
            "admission_us_mean": {
                "endpoint": seed_runs[SEEDS[0]][ENDPOINT[0]]["admission_us_mean"],
                "host_sw": seed_runs[SEEDS[0]][HOST_SW[0]]["admission_us_mean"],
            },
            "transport_us_mean": {
                "endpoint": seed_runs[SEEDS[0]][ENDPOINT[0]]["transport_us_mean"],
                "host_sw": seed_runs[SEEDS[0]][HOST_SW[0]]["transport_us_mean"],
            },
        }
        # Secondary variant (paper wording is "host/GPU pre-scoring").
        for label, _, _ in SECONDARY:
            sg_toks = [seed_runs[s][label]["tok_per_s_mean"] for s in SEEDS]
            sg_m, sg_ci = mean_ci95(sg_toks)
            g2, d2 = gap(ep_m, sg_m)
            row.setdefault("secondary_variants", {})[label] = {
                "toks_mean": sg_m, "toks_ci95": sg_ci,
                "gap_pct_vs_endpoint": g2, "direction": d2,
            }
        per_bw.append(row)
        print(f"  {bw:>4.1f} GB/s | endpoint {ep_m:8.2f} ±{ep_ci:.3f} | "
              f"host-sw {hs_m:8.2f} ±{hs_ci:.3f} tok/s | "
              f"gap {g:+.3f}% ({direction})")

    primary = next(r for r in per_bw if r["cxl_bw_gbs"] == PRIMARY_BW_GBS)

    result = {
        "config": {
            "n_hosts": N_HOSTS,
            "oversubscription": OVERSUB,
            "n_candidates": int(BUDGET * OVERSUB),
            "budget_per_step": BUDGET,
            "n_steps": N_STEPS,
            "seeds": SEEDS,
            "bandwidths_gbs": BANDWIDTHS_GBS,
            "primary_bandwidth_gbs": PRIMARY_BW_GBS,
            "operating_point_from": "experiments/run_host_prescore.py:STARVED",
            "decode_compute_us": STARVED["decode_compute_us"],
            "decode_slack_us": STARVED["decode_slack_us"],
            "useful_fraction": STARVED["useful_fraction"],
            "top_k_useful": STARVED["top_k_useful"],
            "cfo_dedup_frac": 0.0,
            "endpoint_boundary": ENDPOINT[1], "host_sw_boundary": HOST_SW[1],
            "scorer_both_paths": ENDPOINT[2],
            "generator": "simcxl_ext/cxl_admission_sim.py:simulate_step/"
                          "run_closed_loop (same functions as "
                          "experiments/run_host_prescore.py)",
        },
        # Headline fields at the primary bandwidth point.
        "endpoint_toks_mean": primary["endpoint_toks_mean"],
        "endpoint_toks_ci95": primary["endpoint_toks_ci95"],
        "host_sw_toks_mean": primary["host_sw_toks_mean"],
        "host_sw_toks_ci95": primary["host_sw_toks_ci95"],
        "gap_pct": primary["gap_pct"],
        "direction": primary["direction"],
        "per_bandwidth": per_bw,
        "notes": [
            "CIs collapse toward 0 because at 1x oversubscription the "
            "closed-form model is byte-deterministic: every step admits all "
            "64 candidates on both paths, so the seed only perturbs scorer "
            "inputs (Recovery@K), not admitted byte counts or wall time.",
            "The paper's quoted absolute level (128 vs. 130 tok/s) is NOT "
            "reproduced by this operating point: with decode_compute_us=2000 "
            "the compute ceiling is 500 tok/s and the 4 GB/s link caps both "
            "paths near 377 tok/s. The claim's ~1% RELATIVE gap is what this "
            "experiment tests.",
            "No shim required: SimConfig expresses n_hosts=1 and "
            "n_candidates==budget directly; n_hosts does not enter the "
            "cefe/host_prescore throughput path (only two_phase/cefe_passive).",
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print("-" * 78)
    print(f"Primary ({PRIMARY_BW_GBS} GB/s): endpoint "
          f"{primary['endpoint_toks_mean']:.2f} vs host-sw "
          f"{primary['host_sw_toks_mean']:.2f} tok/s -> gap "
          f"{primary['gap_pct']:+.3f}% ({primary['direction']})")
    print(f"Saved data: {OUT_PATH}")


if __name__ == "__main__":
    main()
