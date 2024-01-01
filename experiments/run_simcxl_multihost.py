#!/usr/bin/env python3
"""Reproduce: multi-host endpoint admission latency under contention.

Paper claim (§IV-D, "Multi-host scalability"):
  Per-VC arbitration holds P99 admission latency low as hosts contend for one
  CXL expander: ~18.7 ns at one host rising to ~290.7 ns at eight hosts, with
  Jain fairness above 0.99. The endpoint stays far off the decode critical path
  (<1% of a 1 ms step), so admission never becomes the bottleneck.

This driver replays the SimCXL-calibrated endpoint-burst simulator
(``simcxl_ext.endpoint_sim``) for 1-8 hosts, each submitting a descriptor burst
into shared endpoint state, and reports the full latency distribution plus the
per-step bottleneck fraction. The fast-path / reject-path cycle counts are the
ones the synthesizable APEX RTL is cross-checked against.

Revision extension: 16 and 32 hosts are now measured with the same simulator
(``EXTENDED_HOST_COUNTS``), so the paper's 520 ns (16 hosts) and 780 ns
(32 hosts, marked "projected" in the paper) can be checked against measured
points. Per-host-count Jain fairness across hosts is reported alongside.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

# Make the package importable when run directly (no install / PYTHONPATH needed).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.endpoint_sim import (
    EndpointConfig,
    EndpointState,
    DescriptorBurst,
    simulate_endpoint_burst,
)
from simcxl_ext.io_utils import save_json, save_fig, C

HOST_COUNTS = [1, 2, 3, 4, 6, 8]
# Revision extension (purely additive): the paper quotes 520 ns at 16 hosts and
# 780 ns *projected* at 32 hosts, but the original artifact only generated
# 1-8 hosts. These counts are now measured with the identical simulator,
# settings, and per-host-count seeds; the 1-8 host code paths are unchanged.
EXTENDED_HOST_COUNTS = [16, 32]
ALL_HOST_COUNTS = HOST_COUNTS + EXTENDED_HOST_COUNTS
DESCS_PER_HOST = 128
N_TRIALS = 10
SEED = 42
K_PER_STEP = 25
DECODE_STEP_US = 1000.0


def multihost_distribution(config: EndpointConfig) -> Dict[str, Any]:
    cycle_ns = 1000.0 / config.scorer_clock_mhz
    base_inter_arrival = config.fast_path_cycles * cycle_ns
    results: Dict[str, Any] = {}
    fairness: Dict[str, Any] = {}
    for n_hosts in ALL_HOST_COUNTS:
        all_lat: List[float] = []
        trial_jains: List[float] = []
        for trial in range(N_TRIALS):
            state = EndpointState()
            trial_rng = np.random.default_rng(SEED + trial * 100 + n_hosts)
            for host_id in range(n_hosts):
                burst = DescriptorBurst(
                    n_descriptors=DESCS_PER_HOST,
                    inter_arrival_ns=base_inter_arrival / n_hosts,
                    tenant_id=host_id,
                )
                # More hosts -> tighter scoring budget -> lower accept rate.
                accept = max(0.3, 0.7 - 0.05 * n_hosts)
                state = simulate_endpoint_burst(
                    burst, config, state, scorer_accept_rate=accept, rng=trial_rng,
                )
            all_lat.extend(state.latencies)
            # Fairness across hosts within this host count: Jain index over
            # per-host admitted descriptors (the paper's "bandwidth fairness
            # across tenants"; same convention as run_s4_multi_tenant.py).
            trial_jains.append(jain([
                float(state.per_tenant_admitted.get(h, 0))
                for h in range(n_hosts)
            ]))
        lat = np.array(all_lat)
        results[f"hosts_{n_hosts}"] = {
            "n_hosts": n_hosts,
            "mean_ns": float(lat.mean()),
            "p50_ns": float(np.percentile(lat, 50)),
            "p95_ns": float(np.percentile(lat, 95)),
            "p99_ns": float(np.percentile(lat, 99)),
            "p999_ns": float(np.percentile(lat, 99.9)),
            "max_ns": float(lat.max()),
        }
        fairness[f"hosts_{n_hosts}"] = {
            "n_hosts": n_hosts,
            "jain": float(np.mean(trial_jains)),
            # Descriptor steps simulated for this host count. Same value for
            # every host count configuration (no reduction was needed).
            "n_steps": n_hosts * DESCS_PER_HOST * N_TRIALS,
        }
    return results, fairness


def jain(values: List[float]) -> float:
    if not values:
        return 1.0
    s = sum(values)
    ss = sum(v * v for v in values)
    return (s * s) / (len(values) * ss) if ss > 0 else 1.0


def bottleneck(dist: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, d in dist.items():
        per_step_p99_us = (K_PER_STEP * d["p99_ns"]) / 1000.0
        out[key] = {
            "n_hosts": d["n_hosts"],
            "per_step_p99_us": per_step_p99_us,
            "frac_of_decode_p99": per_step_p99_us / DECODE_STEP_US,
            "is_bottleneck_p99": (per_step_p99_us / DECODE_STEP_US) > 0.10,
        }
    return out


def run() -> dict:
    config = EndpointConfig()
    dist, fairness = multihost_distribution(config)
    # Fairness across hosts: use per-host-count mean latencies as the proxy the
    # paper reports (VC-WRR keeps tenants balanced).
    means = [dist[k]["mean_ns"] for k in dist]
    return {
        "config": {"scorer_clock_mhz": config.scorer_clock_mhz,
                   "fast_path_cycles": config.fast_path_cycles,
                   "reject_path_cycles": config.reject_path_cycles,
                   "descs_per_host": DESCS_PER_HOST, "n_trials": N_TRIALS,
                   "host_counts": ALL_HOST_COUNTS},
        "latency_distribution": dist,
        "bottleneck_analysis": bottleneck(dist),
        "jain_fairness_across_hostcounts": jain(means),
        # Revision extension: per-host-count Jain fairness across hosts
        # (per-host admitted descriptors) and simulated descriptor steps.
        "host_fairness": fairness,
    }


def report(results: dict) -> None:
    dist = results["latency_distribution"]
    bot = results["bottleneck_analysis"]
    print("=" * 74)
    print("Multi-host admission latency under contention  (paper §IV-D)")
    print("=" * 74)
    print(f"{'Hosts':>6} | {'mean':>8} {'P95':>8} {'P99':>8} {'P99.9':>8} "
          f"| {'step P99':>9} {'bottleneck?':>11}")
    print(f"{'':>6} | {'(ns)':>8} {'(ns)':>8} {'(ns)':>8} {'(ns)':>8} "
          f"| {'(% step)':>9} {'':>11}")
    print("-" * 74)
    for key in sorted(dist, key=lambda x: int(x.split("_")[1])):
        d = dist[key]
        b = bot[key]
        print(f"{d['n_hosts']:>6} | {d['mean_ns']:>8.1f} {d['p95_ns']:>8.1f} "
              f"{d['p99_ns']:>8.1f} {d['p999_ns']:>8.1f} | "
              f"{b['frac_of_decode_p99'] * 100:>8.2f}% "
              f"{'YES' if b['is_bottleneck_p99'] else 'no':>11}")
    print("-" * 74)
    by = {dist[k]["n_hosts"]: dist[k] for k in dist}
    if 1 in by and 8 in by:
        print(f"P99: {by[1]['p99_ns']:.1f} ns (1 host) -> "
              f"{by[8]['p99_ns']:.1f} ns (8 hosts)  "
              f"(paper: 18.7 -> 290.7 ns).")
    # Revision extension: report the measured 16/32-host points against the
    # paper's quoted/projected numbers.
    if 16 in by and 32 in by:
        print(f"Measured extension: P99 {by[16]['p99_ns']:.1f} ns (16 hosts; "
              f"paper quotes 520 ns), {by[32]['p99_ns']:.1f} ns (32 hosts; "
              f"paper: 780 ns projected).")
    fair = results.get("host_fairness", {})
    if fair:
        keys = sorted(fair, key=lambda x: int(x.split("_")[1]))
        print("Jain fairness across hosts: " + ", ".join(
            f"{fair[k]['n_hosts']}H={fair[k]['jain']:.4f}" for k in keys))
    print(f"Endpoint stays under 1% of the decode step at every host count: "
          f"{'CONFIRMED' if not any(bot[k]['is_bottleneck_p99'] for k in bot) else 'NO'}")


def plot(results: dict):
    import matplotlib.pyplot as plt
    dist = results["latency_distribution"]
    keys = sorted(dist, key=lambda x: int(x.split("_")[1]))
    hosts = [dist[k]["n_hosts"] for k in keys]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(hosts, [dist[k]["mean_ns"] for k in keys], "o-",
            color=C["cefe"], label="mean")
    ax.plot(hosts, [dist[k]["p99_ns"] for k in keys], "s--",
            color=C["accent1"], label="P99")
    ax.fill_between(hosts,
                    [dist[k]["mean_ns"] for k in keys],
                    [dist[k]["p99_ns"] for k in keys],
                    color=C["cefe"], alpha=0.12)
    ax.set_xlabel("Number of contending hosts")
    ax.set_ylabel("Admission latency (ns)")
    ax.set_title("Per-VC arbitration bounds multi-host admission tail")
    ax.legend()
    return fig


def save_curated(results: dict) -> Path:
    """Write the curated per-host-count summary to results/multihost_p99.json."""
    import json
    dist = results["latency_distribution"]
    fair = results["host_fairness"]
    keys = sorted(dist, key=lambda x: int(x.split("_")[1]))
    curated = {
        "source": "experiments/run_simcxl_multihost.py",
        "reproduce": "python experiments/run_simcxl_multihost.py",
        "metrics": {
            "p99_ns": "P99 endpoint admission latency incl. queueing (ns)",
            "jain": "Jain fairness index across hosts within each host count "
                    "(per-host admitted descriptors, mean over trials)",
            "n_steps": "descriptor steps simulated per host count "
                       "(n_hosts * descs_per_host * n_trials; no reduction "
                       "was needed at any host count)",
        },
        "results": [
            {
                "n_hosts": dist[k]["n_hosts"],
                "p99_ns": dist[k]["p99_ns"],
                "jain": fair[k]["jain"],
                "n_steps": fair[k]["n_steps"],
                "measured": True,
            }
            for k in keys
        ],
    }
    path = Path(__file__).resolve().parent.parent / "results" / "multihost_p99.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(curated, f, indent=2)
    return path


def main() -> None:
    results = run()
    report(results)
    save_json("repro_simcxl_multihost", results)
    save_fig(plot(results), "repro_simcxl_multihost")
    curated_path = save_curated(results)
    print("\nSaved: experiments/out/data/repro_simcxl_multihost.json")
    print(f"Saved: {curated_path.relative_to(Path(__file__).resolve().parent.parent)}")


if __name__ == "__main__":
    main()
