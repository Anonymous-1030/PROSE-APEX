#!/usr/bin/env python3
"""Paired baseline sweep: replay ONE shared trajectory per (workload, seed)
through all seven mechanisms, emit request-level JSONL + run-level CSV.

Fairness contract (see README §Fairness):
  * For each (workload, seed) exactly one EventTrace is generated (all RNG lives
    in generate_trace). Every mechanism replays that identical trace.
  * No mechanism sees a different arrival order, eviction decision, slot reuse,
    or transfer length. The only per-mechanism divergence is its protection /
    validation semantics, read from its MechanismSpec.

Outputs:
  results/baselines/raw/<workload>_seed<seed>_<method>.jsonl   (request-level)
  results/baselines/summary_by_run.csv                          (run-level)
  results/baselines/manifest.json                               (provenance)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import experiments.baselines as B
from experiments.baselines.baseline_common import (
    BaselineConfig, generate_trace, replay_run,
    valid_throughput, stale_mib_per_gib, control_header_overhead_pct,
    METHOD_ORDER, SEGMENT_SIZES,
)

HERE = Path(__file__).resolve().parent
DEFAULT_CFG = HERE / "configs" / "baseline_sweep.yaml"
RESULTS = ROOT / "results" / "baselines"
RAW = RESULTS / "raw"

# run-level CSV columns (fixed order)
RUN_COLUMNS = [
    "workload", "seed", "method", "segment_bytes",
    "makespan_ns", "valid_throughput_gbps",
    "total_requested_bytes", "total_valid_bytes", "total_stale_bytes",
    "total_wire_bytes", "total_control_bytes", "total_header_bytes",
    "stale_mib_per_gib", "control_header_overhead_pct",
    "completed_valid_requests", "rejected_requests", "aborted_requests",
    "rpe_events", "extra_rtt", "serialized_acquire_ns",
    "pin_span_ratio_median", "pin_span_ratio_p95",
    "evict_attempts_queue", "evict_attempts_xfer", "evict_attempts_total",
    "evict_fired", "evict_blocked",
    "queue_reclaim", "n_requests",
]

# request-level JSONL field order (spec §V)
REQ_FIELDS = [
    "method", "segment_bytes", "host_id", "request_id", "object_id",
    "expected_epoch", "observed_epoch_at_enqueue", "observed_epoch_at_admission",
    "slot_id", "slot_key", "object_bytes", "requested_bytes",
    "valid_payload_bytes", "stale_payload_bytes", "wire_payload_bytes",
    "control_bytes", "header_bytes",
    "descriptor_enqueue_ns", "protection_acquire_ns", "endpoint_admission_ns",
    "first_payload_issue_ns", "last_payload_complete_ns", "protection_release_ns",
    "reject_ns", "abort_ns", "extra_round_trips", "rpe_event",
    "reclaimed_while_queued",
]


def build_config(defaults: Dict[str, Any], workload: Dict[str, Any],
                 n_requests: int | None) -> BaselineConfig:
    """Merge defaults + per-workload overrides into a BaselineConfig."""
    valid = {f.name for f in fields(BaselineConfig)}
    params: Dict[str, Any] = {k: v for k, v in defaults.items() if k in valid}
    for k, v in workload.items():
        if k in valid:
            params[k] = v
    params["name"] = workload["name"]
    if n_requests is not None:
        params["n_requests"] = n_requests
    return BaselineConfig(**params)


def run_sweep(cfg_path: Path, n_requests: int | None,
              seeds_override: List[int] | None,
              results_dir: Path = RESULTS,
              methods: List[str] | None = None) -> Dict[str, Any]:
    with cfg_path.open(encoding="utf-8") as f:
        spec_cfg = yaml.safe_load(f)

    seeds = seeds_override or spec_cfg["seeds"]
    defaults = spec_cfg["defaults"]
    workloads = spec_cfg["workloads"]
    method_names = methods or METHOD_ORDER
    specs = [B.SPECS[m] for m in method_names]

    raw_dir = results_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    run_rows: List[Dict[str, Any]] = []
    n_runs = 0
    failed: List[str] = []

    for wl in workloads:
        wname = wl["name"]
        for seed in seeds:
            config = build_config(defaults, wl, n_requests)
            # ONE shared trajectory for this (workload, seed).
            trace = generate_trace(config, wname, seed)
            for spec in specs:
                run_id = f"{wname}_seed{seed}_{spec.name}"
                try:
                    summary = replay_run(
                        trace, spec,
                        check_prose_invariant=(spec.name == "PROSE"))
                except Exception as exc:  # pragma: no cover - defensive
                    failed.append(f"{run_id}: {exc}")
                    continue

                # request-level JSONL
                jpath = raw_dir / f"{run_id}.jsonl"
                with jpath.open("w", encoding="utf-8") as jf:
                    for r in summary["rows"]:
                        jf.write(json.dumps(
                            {"run_id": run_id, "workload": wname, "seed": seed,
                             **{k: r[k] for k in REQ_FIELDS}},
                            separators=(",", ":")) + "\n")

                run_rows.append({
                    "workload": wname,
                    "seed": seed,
                    "method": spec.name,
                    "segment_bytes": summary["segment_bytes"],
                    "makespan_ns": summary["makespan_ns"],
                    "valid_throughput_gbps": valid_throughput(summary),
                    "total_requested_bytes": summary["total_requested_bytes"],
                    "total_valid_bytes": summary["total_valid_bytes"],
                    "total_stale_bytes": summary["total_stale_bytes"],
                    "total_wire_bytes": summary["total_wire_bytes"],
                    "total_control_bytes": summary["total_control_bytes"],
                    "total_header_bytes": summary["total_header_bytes"],
                    "stale_mib_per_gib": stale_mib_per_gib(summary),
                    "control_header_overhead_pct": control_header_overhead_pct(summary),
                    "completed_valid_requests": summary["completed_valid_requests"],
                    "rejected_requests": summary["rejected_requests"],
                    "aborted_requests": summary["aborted_requests"],
                    "rpe_events": summary["rpe_events"],
                    "extra_rtt": summary["extra_rtt"],
                    "serialized_acquire_ns": summary["serialized_acquire_ns"],
                    "pin_span_ratio_median": summary["pin_span_ratio_median"],
                    "pin_span_ratio_p95": summary["pin_span_ratio_p95"],
                    "evict_attempts_queue": summary["evict_attempts_queue"],
                    "evict_attempts_xfer": summary["evict_attempts_xfer"],
                    "evict_attempts_total": summary["evict_attempts_total"],
                    "evict_fired": summary["evict_fired"],
                    "evict_blocked": summary["evict_blocked"],
                    "queue_reclaim": summary["queue_reclaim"],
                    "n_requests": summary["n_requests"],
                })
                n_runs += 1
            print(f"[{wname} seed={seed}] {len(specs)} mechanisms replayed")

    # write run-level CSV (fixed row order = workload, seed, METHOD_ORDER)
    order = {m: i for i, m in enumerate(METHOD_ORDER)}
    run_rows.sort(key=lambda r: (r["workload"], r["seed"], order[r["method"]]))
    csv_path = results_dir / "summary_by_run.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as cf:
        w = csv.DictWriter(cf, fieldnames=RUN_COLUMNS)
        w.writeheader()
        for row in run_rows:
            w.writerow(row)

    # Each (workload, seed) pair yields one paired sample per mechanism (each
    # mechanism replays the same trace, normalized to its own paired Unsafe run).
    n_paired_samples = len(workloads) * len(seeds)
    manifest = {
        "config_file": str(cfg_path),
        "seeds": list(seeds),
        "workloads": [wl["name"] for wl in workloads],
        "methods": method_names,
        "segment_sizes": SEGMENT_SIZES,
        "n_runs": n_runs,
        "n_paired_samples_per_method": n_paired_samples,
        "paired_sample_axes": "workload x seed",
        "failed_runs": failed,
        "defaults": defaults,
        "n_requests_override": n_requests,
        "raw_dir": str(raw_dir),
        "summary_by_run_csv": str(csv_path),
        "fairness": ("one EventTrace per (workload,seed) generated once with all "
                     "RNG; every mechanism replays the identical trace"),
    }
    with (results_dir / "manifest.json").open("w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2)

    print(f"\nRuns: {n_runs}  Failed: {len(failed)}")
    print(f"CSV : {csv_path}")
    print(f"Raw : {raw_dir}")
    if failed:
        print("FAILED RUNS:")
        for fr in failed:
            print("  " + fr)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=DEFAULT_CFG)
    ap.add_argument("--n-requests", type=int, default=None,
                    help="Override requests per run (default from config).")
    ap.add_argument("--seeds", type=int, nargs="+", default=None,
                    help="Override seed list (default from config).")
    args = ap.parse_args()
    run_sweep(args.config, args.n_requests, args.seeds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
