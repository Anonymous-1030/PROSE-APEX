#!/usr/bin/env python3
"""Placement-isolating experiment: Host-Side vs Host-Credit vs Endpoint.

Addresses W1: demonstrates that the endpoint's value comes from *placement*
(seeing all tenants' queues), not from *scoring quality* (which is identical).

All three placements use the same Core2 scorer (odus_x with 2 active experts).
The difference is WHERE the binding decision runs:
  - host_side (Opt): each host scores independently using its LOCAL view of
    state. Under multi-host, each host cannot observe other hosts' concurrent
    admits, creating a "divergence window" where multiple hosts admit the same
    chunk or exceed the shared quota.
  - credit (Cred): CXL 3.1 host-credit flow control. Per-VC reservation bounds
    in-flight descriptors and prevents queue overflow, so the global budget is
    enforced and bandwidth stays fair — but credits regulate queue occupancy,
    not object identity. With no residency check at endpoint dequeue,
    device-side eviction and epoch rollover between enqueue and admission
    produce stale admits invisible to hosts (grounded in the calibrated Mode-C
    decomposition of simcxl_ext/cxl_admission_sim.py).
  - endpoint (EP): the CEFE hardware scores with a GLOBAL view, serializing
    all hosts' descriptors through a single pipeline and checking residency
    at dequeue.

Metrics (computed for EVERY placement):
  1. stale_admission_rate: fraction of admits that are stale at service time
  2. jain_fairness: Jain's fairness index of per-host admitted bandwidth
  3. mis_bound_bw_MB: wasted payload bandwidth (mis-bound descriptors x
     64 KiB, cumulative MB)
  4. recovery_at_k: should be identical (same scorer), confirming placement
     helps coordination, not quality

Usage:
    python experiments/run_placement_isolation.py
"""
from __future__ import annotations

import sys
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.cxl_admission_sim import (
    SimConfig, StepGroundTruth, synth_step, SCORER_REGISTRY,
    CHUNK_PAYLOAD_B, META_B,
)
from simcxl_ext.io_utils import save_json, save_fig, C

import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST_COUNTS = [1, 2, 4, 8, 16]
N_STEPS = 256
SEED = 42
BUDGET_PER_STEP = 64   # shared global budget (chunks)
N_CANDIDATES = 1024
K_USEFUL = 32
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


@dataclass
class PlacementResult:
    n_hosts: int
    # Host-side metrics
    host_stale_rate: float
    host_jain: float
    host_mis_bound_bw_MB: float
    host_recovery_at_k: float
    # Host-credit (CXL 3.1 per-VC reservation) metrics
    cred_stale_rate: float
    cred_jain: float
    cred_mis_bound_bw_MB: float
    cred_recovery_at_k: float
    # Endpoint metrics
    ep_stale_rate: float
    ep_jain: float
    ep_mis_bound_bw_MB: float
    ep_recovery_at_k: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def jain_index(values: List[float]) -> float:
    """Jain's fairness index: 1.0 = perfectly fair."""
    if not values or all(v == 0 for v in values):
        return 1.0
    s = sum(values)
    ss = sum(v * v for v in values)
    n = len(values)
    return (s * s) / (n * ss) if ss > 0 else 1.0


def score_candidates(step: StepGroundTruth, query_dir: np.ndarray) -> np.ndarray:
    """Score using odus_x (the Core2 scorer)."""
    return SCORER_REGISTRY["odus_x"](step, query_dir=query_dir, odus_weights=None)


# ---------------------------------------------------------------------------
# Host-side baseline: each host scores independently with LOCAL state
# ---------------------------------------------------------------------------
def simulate_host_side(
    step: StepGroundTruth,
    query_dir: np.ndarray,
    n_hosts: int,
    budget: int,
    rng: np.random.Generator,
) -> Dict[str, Any]:
    """Simulate host-side scoring under multi-host divergence.

    Each host independently computes scores and picks its top-budget chunks
    from the FULL candidate set (each host believes it owns the full budget).
    Because hosts cannot observe each other's concurrent decisions:
    - Multiple hosts admit the same chunk (stale/duplicate)
    - Unique admits may exceed the global budget (mis-bound BW)
    """
    n = len(step.candidate_ids)
    scores = score_candidates(step, query_dir)

    per_host_admits: List[np.ndarray] = []

    for _ in range(n_hosts):
        noise_scale = 0.03 * (n_hosts - 1)
        local_scores = scores + rng.normal(0, noise_scale, n)
        order = np.argsort(local_scores)[::-1]
        admits = order[:budget]  # each host thinks it has FULL budget
        per_host_admits.append(admits)

    # Compute collisions: same chunk admitted by multiple hosts
    all_admits = np.concatenate(per_host_admits)
    unique_admits, counts = np.unique(all_admits, return_counts=True)
    total_admit_attempts = len(all_admits)
    duplicate_attempts = int(counts.sum() - len(unique_admits))

    # In reality only `budget` total chunks can be promoted.
    # Hosts that admit beyond the global quota waste bandwidth.
    actual_unique = len(unique_admits)
    mis_bound_chunks = max(0, actual_unique - budget)
    mis_bound_bw = mis_bound_chunks * CHUNK_PAYLOAD_B

    # Fairness: how many of each host's admits survived the global cut?
    global_order = np.argsort(scores)[::-1]
    global_admitted_set = set(int(global_order[i]) for i in range(min(budget, n)))
    per_host_effective = []
    for h_admits in per_host_admits:
        effective = sum(1 for c in h_admits if int(c) in global_admitted_set)
        per_host_effective.append(float(effective))

    # Recovery@K
    top_useful = set(int(x) for x in step.useful_ids[:K_USEFUL])
    admitted_useful = global_admitted_set & top_useful
    recovery = len(admitted_useful) / max(1, len(top_useful))

    stale_rate = duplicate_attempts / max(1, total_admit_attempts)

    return {
        "stale_rate": stale_rate,
        "jain": jain_index(per_host_effective),
        "mis_bound_bw": mis_bound_bw,
        "recovery_at_k": recovery,
    }


# ---------------------------------------------------------------------------
# CXL 3.1 host-credit baseline: per-VC reservation, no residency check
# ---------------------------------------------------------------------------
# Calibrated Mode-C constants, reused as-is from the admission sim (the
# `cefe_passive` boundary): residual staleness decomposes into an eviction
# race (0.111) plus an epoch rollover (0.033), gated by the cross-host factor
# (1 - 1/H). Do NOT re-hardcode these numbers here.
_MODEC_CFG = SimConfig()


def simulate_credit(
    step: StepGroundTruth,
    query_dir: np.ndarray,
    n_hosts: int,
    budget: int,
) -> Dict[str, Any]:
    """Simulate CXL 3.1 host-credit flow control (per-VC reservation).

    Credits bound each VC's in-flight descriptors and flow control prevents
    queue overflow, so — unlike host-optimistic shadow state — hosts cannot
    double-admit a chunk or overrun the shared quota: the endpoint dequeues
    exactly `budget` descriptors per step, split evenly across VCs (fair by
    construction, Jain = 1.0).

    But credits regulate queue OCCUPANCY, not object identity: there is no
    residency check at endpoint dequeue, so a descriptor that was valid at
    enqueue can be stale at service time — device-side eviction or an epoch
    rollover between enqueue and admission invalidates it, invisibly to the
    host until after commit. The residual stale fraction is grounded in the
    calibrated Mode-C decomposition (cxl_admission_sim.py, `cefe_passive`):

        stale_frac(H) = (evict_race + epoch_roll) * (1 - 1/H)

    i.e. single-host credit flow control is safe (no cross-host race), and
    the residual saturates toward 0.111 + 0.033 = 14.4% as H grows. Stale
    admits still move their 64 KiB payload, which is the wasted (mis-bound)
    bandwidth — the same per-descriptor accounting as the host-side column.
    """
    n = len(step.candidate_ids)
    scores = score_candidates(step, query_dir)

    h = max(1, int(n_hosts))
    host_gate = 1.0 - 1.0 / h   # cross-host race only exists for h > 1
    stale_frac = min(0.999, (_MODEC_CFG.passive_evict_race_frac
                             + _MODEC_CFG.passive_epoch_roll_frac) * host_gate)

    # The endpoint dequeues exactly `budget` descriptors per step (credits
    # enforce the global budget); `stale_frac` of them are stale at service.
    order = np.argsort(scores)[::-1]
    admitted = set(int(order[i]) for i in range(min(budget, n)))
    stale_admits = budget * stale_frac
    mis_bound_bw = stale_admits * CHUNK_PAYLOAD_B  # stale payload still moves

    # Per-VC reservation gives every host an equal credit share.
    per_vc = budget // h
    remainder = budget % h
    per_host_effective = [float(per_vc + (1 if i < remainder else 0))
                          for i in range(h)]

    # Recovery@K: same scorer, same global top-budget set as the endpoint.
    top_useful = set(int(x) for x in step.useful_ids[:K_USEFUL])
    admitted_useful = admitted & top_useful
    recovery = len(admitted_useful) / max(1, len(top_useful))

    return {
        "stale_rate": stale_frac,
        "jain": jain_index(per_host_effective),
        "mis_bound_bw": mis_bound_bw,
        "recovery_at_k": recovery,
    }


# ---------------------------------------------------------------------------
# Endpoint baseline: single global pipeline, serialized decisions
# ---------------------------------------------------------------------------
def simulate_endpoint(
    step: StepGroundTruth,
    query_dir: np.ndarray,
    n_hosts: int,
    budget: int,
) -> Dict[str, Any]:
    """Simulate endpoint scoring with global view.

    All hosts' descriptors are serialized through one pipeline.
    The endpoint sees the global residency state, so:
    - No stale admits (it knows what's already admitted)
    - No mis-bound (it enforces the global budget exactly)
    - Perfect fairness by WRR across VCs
    """
    n = len(step.candidate_ids)
    scores = score_candidates(step, query_dir)

    # Endpoint scores globally and picks top-budget
    order = np.argsort(scores)[::-1]
    admitted = set(int(order[i]) for i in range(min(budget, n)))

    # Fair distribution across hosts via WRR
    per_host_budget = budget // n_hosts
    remainder = budget % n_hosts
    per_host_effective = []
    for h in range(n_hosts):
        alloc = per_host_budget + (1 if h < remainder else 0)
        per_host_effective.append(float(alloc))

    # Recovery@K
    top_useful = set(int(x) for x in step.useful_ids[:K_USEFUL])
    admitted_useful = admitted & top_useful
    recovery = len(admitted_useful) / max(1, len(top_useful))

    return {
        "stale_rate": 0.0,
        "jain": jain_index(per_host_effective),
        "mis_bound_bw": 0.0,
        "recovery_at_k": recovery,
    }


# ---------------------------------------------------------------------------
# Main experiment loop
# ---------------------------------------------------------------------------
def run_experiment() -> List[PlacementResult]:
    rng = np.random.default_rng(SEED)
    results: List[PlacementResult] = []

    for n_hosts in HOST_COUNTS:
        host_metrics = {"stale_rate": [], "jain": [], "mis_bound_bw": [], "recovery_at_k": []}
        cred_metrics = {"stale_rate": [], "jain": [], "mis_bound_bw": [], "recovery_at_k": []}
        ep_metrics = {"stale_rate": [], "jain": [], "mis_bound_bw": [], "recovery_at_k": []}

        for _ in range(N_STEPS):
            useful_dir = rng.normal(0, 1, 32)
            useful_dir /= np.linalg.norm(useful_dir) + 1e-9
            query_dir = useful_dir + 0.65 * rng.normal(0, 1, 32)
            query_dir /= np.linalg.norm(query_dir) + 1e-9

            step = synth_step(N_CANDIDATES, 0.04, rng,
                              semantic_signal_strength=0.80,
                              useful_dir=useful_dir)

            h_res = simulate_host_side(step, query_dir, n_hosts, BUDGET_PER_STEP, rng)
            c_res = simulate_credit(step, query_dir, n_hosts, BUDGET_PER_STEP)
            e_res = simulate_endpoint(step, query_dir, n_hosts, BUDGET_PER_STEP)

            for k in host_metrics:
                host_metrics[k].append(h_res[k])
                cred_metrics[k].append(c_res[k])
                ep_metrics[k].append(e_res[k])

        results.append(PlacementResult(
            n_hosts=n_hosts,
            host_stale_rate=float(np.mean(host_metrics["stale_rate"])),
            host_jain=float(np.mean(host_metrics["jain"])),
            host_mis_bound_bw_MB=float(np.sum(host_metrics["mis_bound_bw"])) / (1024 * 1024),
            host_recovery_at_k=float(np.mean(host_metrics["recovery_at_k"])),
            cred_stale_rate=float(np.mean(cred_metrics["stale_rate"])),
            cred_jain=float(np.mean(cred_metrics["jain"])),
            cred_mis_bound_bw_MB=float(np.sum(cred_metrics["mis_bound_bw"])) / (1024 * 1024),
            cred_recovery_at_k=float(np.mean(cred_metrics["recovery_at_k"])),
            ep_stale_rate=float(np.mean(ep_metrics["stale_rate"])),
            ep_jain=float(np.mean(ep_metrics["jain"])),
            ep_mis_bound_bw_MB=float(np.sum(ep_metrics["mis_bound_bw"])) / (1024 * 1024),
            ep_recovery_at_k=float(np.mean(ep_metrics["recovery_at_k"])),
        ))

    return results


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def plot(results: List[PlacementResult]) -> plt.Figure:
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    hosts = [r.n_hosts for r in results]

    # (a) Stale admission rate
    ax = axes[0, 0]
    ax.plot(hosts, [r.host_stale_rate * 100 for r in results],
            "o-", color=C["fts"], label="Host-Side")
    ax.plot(hosts, [r.cred_stale_rate * 100 for r in results],
            "^--", color=C["accent2"], label="Host-Credit")
    ax.plot(hosts, [r.ep_stale_rate * 100 for r in results],
            "s-", color=C["cefe"], label="Endpoint")
    ax.set_xlabel("Number of Hosts")
    ax.set_ylabel("Stale Admission Rate (%)")
    ax.set_title("(a) Stale admits at service time")
    ax.legend()
    ax.set_ylim(bottom=0)

    # (b) Jain fairness index
    ax = axes[0, 1]
    ax.plot(hosts, [r.host_jain for r in results],
            "o-", color=C["fts"], label="Host-Side")
    ax.plot(hosts, [r.cred_jain for r in results],
            "^--", color=C["accent2"], label="Host-Credit")
    ax.plot(hosts, [r.ep_jain for r in results],
            "s-", color=C["cefe"], label="Endpoint")
    ax.set_xlabel("Number of Hosts")
    ax.set_ylabel("Jain Fairness Index")
    ax.set_title("(b) Per-host bandwidth fairness")
    ax.legend()
    ax.set_ylim(0.5, 1.05)

    # (c) Mis-bound wasted bandwidth
    ax = axes[1, 0]
    ax.plot(hosts, [r.host_mis_bound_bw_MB for r in results],
            "o-", color=C["fts"], label="Host-Side")
    ax.plot(hosts, [r.cred_mis_bound_bw_MB for r in results],
            "^--", color=C["accent2"], label="Host-Credit")
    ax.plot(hosts, [r.ep_mis_bound_bw_MB for r in results],
            "s-", color=C["cefe"], label="Endpoint")
    ax.set_xlabel("Number of Hosts")
    ax.set_ylabel("Wasted BW (MB, cumulative)")
    ax.set_title("(c) Mis-bound wasted bandwidth")
    ax.legend()
    ax.set_ylim(bottom=0)

    # (d) Recovery@K (should be ~equal)
    ax = axes[1, 1]
    ax.plot(hosts, [r.host_recovery_at_k for r in results],
            "o-", color=C["fts"], label="Host-Side")
    ax.plot(hosts, [r.cred_recovery_at_k for r in results],
            "^--", color=C["accent2"], label="Host-Credit")
    ax.plot(hosts, [r.ep_recovery_at_k for r in results],
            "s-", color=C["cefe"], label="Endpoint")
    ax.set_xlabel("Number of Hosts")
    ax.set_ylabel("Recovery@K")
    ax.set_title("(d) Scoring quality (same scorer)")
    ax.legend()
    ax.set_ylim(0, 1.05)

    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    print("=" * 78)
    print("Placement-Isolating Experiment: Host-Side vs Host-Credit vs Endpoint (W1)")
    print("=" * 78)

    results = run_experiment()

    print(f"\n{'Hosts':<6} {'Stale(O%)':<10} {'Stale(C%)':<10} {'Stale(E%)':<10} "
          f"{'Jain(O)':<8} {'Jain(C)':<8} {'Jain(E)':<8} "
          f"{'MisBW(O)MB':<11} {'MisBW(C)MB':<11} {'MisBW(E)MB':<11}")
    print("-" * 100)
    for r in results:
        print(f"{r.n_hosts:<6} {r.host_stale_rate*100:<10.2f} {r.cred_stale_rate*100:<10.2f} "
              f"{r.ep_stale_rate*100:<10.2f} "
              f"{r.host_jain:<8.4f} {r.cred_jain:<8.4f} {r.ep_jain:<8.4f} "
              f"{r.host_mis_bound_bw_MB:<11.2f} {r.cred_mis_bound_bw_MB:<11.2f} "
              f"{r.ep_mis_bound_bw_MB:<11.2f}")

    # Save data (existing keys preserved; `credit` block added per host count)
    data = {
        "experiment": "placement_isolation_w1",
        "description": "Host-Side-Core2-MultiHost vs CXL-3.1-Host-Credit vs Endpoint-Core2: same scorer, different placement",
        "credit_model": {
            "mechanism": "CXL 3.1 host-credit, per-VC reservation, no endpoint residency check",
            "stale_frac_formula": "(passive_evict_race_frac + passive_epoch_roll_frac) * (1 - 1/H)",
            "passive_evict_race_frac": _MODEC_CFG.passive_evict_race_frac,
            "passive_epoch_roll_frac": _MODEC_CFG.passive_epoch_roll_frac,
            "source": "calibrated Mode-C decomposition (simcxl_ext/cxl_admission_sim.py, cefe_passive)",
        },
        "host_counts": HOST_COUNTS,
        "results": [
            {
                "n_hosts": r.n_hosts,
                "host_side": {
                    "stale_admission_rate": r.host_stale_rate,
                    "jain_fairness": r.host_jain,
                    "mis_bound_bw_MB": r.host_mis_bound_bw_MB,
                    "recovery_at_k": r.host_recovery_at_k,
                },
                "credit": {
                    "stale_admission_rate": r.cred_stale_rate,
                    "jain_fairness": r.cred_jain,
                    "mis_bound_bw_MB": r.cred_mis_bound_bw_MB,
                    "recovery_at_k": r.cred_recovery_at_k,
                },
                "endpoint": {
                    "stale_admission_rate": r.ep_stale_rate,
                    "jain_fairness": r.ep_jain,
                    "mis_bound_bw_MB": r.ep_mis_bound_bw_MB,
                    "recovery_at_k": r.ep_recovery_at_k,
                },
            }
            for r in results
        ],
    }
    save_json("placement_isolation", data)

    # Curated table-shaped copy for the paper (Table V): one row per host
    # count, all three metrics for all three placements.
    full = {
        "experiment": "placement_isolation_full",
        "source": "experiments/out/data/placement_isolation.json",
        "units": {
            "stale": "percent of admits stale at service time",
            "jain": "Jain fairness index (1.0 = perfectly fair)",
            "mis_bound_bw": "MB, cumulative; mis-bound descriptors x 64 KiB",
        },
        "columns": ["hosts", "Stale(Opt)", "Stale(Cred)", "Stale(EP)",
                    "Jain(Opt)", "Jain(Cred)", "Jain(EP)",
                    "MisBW(Opt)_MB", "MisBW(Cred)_MB", "MisBW(EP)_MB"],
        "rows": [
            {
                "hosts": r.n_hosts,
                "Stale(Opt)": r.host_stale_rate * 100,
                "Stale(Cred)": r.cred_stale_rate * 100,
                "Stale(EP)": r.ep_stale_rate * 100,
                "Jain(Opt)": r.host_jain,
                "Jain(Cred)": r.cred_jain,
                "Jain(EP)": r.ep_jain,
                "MisBW(Opt)_MB": r.host_mis_bound_bw_MB,
                "MisBW(Cred)_MB": r.cred_mis_bound_bw_MB,
                "MisBW(EP)_MB": r.ep_mis_bound_bw_MB,
            }
            for r in results
        ],
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_DIR / "placement_isolation_full.json", "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2)

    fig = plot(results)
    save_fig(fig, "placement_isolation")

    print(f"\nOutput: experiments/out/data/placement_isolation.json")
    print(f"Curated: results/placement_isolation_full.json")
    print(f"Figure: experiments/out/figures/placement_isolation.pdf")
    print("\nKey finding: Recovery@K is ~identical (same scorer), but host-side")
    print("suffers stale admits and fairness degradation as hosts increase.")
    print("Host-credit flow control keeps fairness but cannot see device-side")
    print("eviction/epoch rollover, leaving a residual stale rate; only endpoint")
    print("placement holds stale=0. Placement helps coordination, not quality.")


if __name__ == "__main__":
    main()
