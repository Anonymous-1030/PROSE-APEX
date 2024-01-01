#!/usr/bin/env python3
"""Design-space rows for the GenOnly + epoch-fence baseline (paper TODO).

Runs the SAME protocol that produced ``results/design_space.json``'s GENONLY
row — oversub {2,4,8,16,32,64} x 16 tenants x 10 seeds, 512-entry pool,
32-object admit budget, 200 steps, bound mode capacity, paired traces — for
{PROSE, GENONLY, GENONLY_EF} and writes NEW output files (the committed
``results/design_space.json`` is NOT overwritten).

GENONLY_EF is GENONLY plus a Tigon-style epoch fence on the reclaim path: the
slot overwrite is deferred by one grace period (the allocator epoch,
``BaselineConfig.eviction_interval_ns`` = 500 ns). With a 16.6 us transfer the
fence covers only ~3% of the post-request tail, so the measured exposure stays
close to — but strictly below — GENONLY's. See
``experiments/baselines/genonly_epoch_fence.py`` for the full semantics.

Outputs:
  experiments/out/design_space/design_space_epochfence.csv   per-run rows
  results/design_space_epochfence.json                       aggregated comparison
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.oversub_reclaim import (           # noqa: E402
    OversubConfig, generate_oversub_trace, replay_oversub, MECHS,
)

OUT = ROOT / "experiments" / "out" / "design_space"
RESULTS = ROOT / "results"

# ── identical grid to run_design_space.py (capacity bound) ──────────────────
OVERSUB = [2, 4, 8, 16, 32, 64]
TENANTS = 16
SEEDS = list(range(10))
BUDGET = 32
CAPACITY = 512
N_STEPS = 200
BOUND_MODE = "capacity"
MECHS_TO_RUN = ["PROSE", "GENONLY", "GENONLY_EF"]

METRICS = ("valid_throughput_Bpns", "admission_p99_ns", "pinned_peak",
           "reclaimable_capacity_frac", "rpe_payload_frac", "stale_admit_rate",
           "retries_per_descriptor", "evict_fired", "evict_blocked")


def run_grid() -> list:
    rows = []
    for oversub in OVERSUB:
        for seed in SEEDS:
            cfg = OversubConfig(
                oversubscription=oversub, n_tenants=TENANTS,
                admit_budget=BUDGET, n_steps=N_STEPS, capacity=CAPACITY,
                token_table=CAPACITY, bound_mode=BOUND_MODE, seed=seed,
            )
            trace = generate_oversub_trace(cfg)
            for m in MECHS_TO_RUN:
                rows.append(replay_oversub(trace, MECHS[m]))
    return rows


def aggregate(rows: list) -> dict:
    buckets = {}
    for r in rows:
        buckets.setdefault((r["mechanism"], r["oversubscription"]), []).append(r)
    agg = {}
    for k, rs in buckets.items():
        agg[k] = {kk: float(mean(r[kk] for r in rs)) for kk in METRICS}
        agg[k]["throughput_std"] = (
            float(pstdev([r["valid_throughput_Bpns"] for r in rs]))
            if len(rs) > 1 else 0.0)
    return agg


def check_reproduction(agg: dict) -> int:
    """GENONLY/PROSE means must match the committed design_space.json (the
    grid, seeds, and paired traces are identical, so the replay is exact)."""
    committed = json.loads((RESULTS / "design_space.json").read_text())
    grid = committed["table_iv_grid_capacity"]
    mismatches = 0
    for m in ("GENONLY", "PROSE"):
        for o in OVERSUB:
            for kk in ("rpe_payload_frac", "valid_throughput_Bpns",
                       "pinned_peak", "reclaimable_capacity_frac"):
                old = float(grid[m][str(o)][kk])
                new = agg[(m, o)][kk]
                if abs(old - new) > 1e-9:
                    print(f"MISMATCH {m} {o}x {kk}: committed={old} rerun={new}")
                    mismatches += 1
    print(f"reproduction check vs committed design_space.json: "
          f"{mismatches} mismatches")
    return mismatches


def main() -> int:
    rows = run_grid()
    agg = aggregate(rows)

    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["bound_mode", "mechanism", "oversubscription", "n_tenants", "seed",
            "valid_throughput_Bpns", "admission_p99_ns", "pinned_peak",
            "reclaimable_capacity_frac", "rpe_payload_frac", "stale_admits",
            "stale_admit_rate", "retries", "retries_per_descriptor",
            "evict_fired", "evict_blocked"]
    with open(OUT / "design_space_epochfence.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    rc = check_reproduction(agg)

    prose_base = {o: agg[("PROSE", o)]["valid_throughput_Bpns"] for o in OVERSUB}
    curated = {
        "experiment": "design_space_epochfence",
        "config": {"oversub": OVERSUB, "n_tenants": TENANTS, "seeds": SEEDS,
                   "admit_budget": BUDGET, "pool": CAPACITY, "n_steps": N_STEPS,
                   "bound_mode": BOUND_MODE, "mechanisms": MECHS_TO_RUN},
        "grace_period": {
            "semantics": ("Tigon-style EBR: unlink (generation bump) visible to "
                          "admission checks immediately == GENONLY; slot "
                          "overwrite deferred by one grace period, so a raced "
                          "transfer stays valid for grace_ns beyond the reclaim "
                          "request"),
            "value_ns": 500.0,
            "source": "BaselineConfig.eviction_interval_ns (allocator reuse epoch)",
            "transfer_service_ns": 65536 / 4.0 + 2 * 15.0 + 50.0 + 120.0,
            "stale_tail_frac_per_raced_transfer": max(
                0.0, 0.5 - 500.0 / (65536 / 4.0 + 2 * 15.0 + 50.0 + 120.0)),
        },
        "grid_capacity": {
            m: {str(o): agg[(m, o)] for o in OVERSUB} for m in MECHS_TO_RUN
        },
        "valid_thr_norm_to_prose_capacity": {
            m: {str(o): (agg[(m, o)]["valid_throughput_Bpns"] / prose_base[o]
                         if prose_base[o] > 0 else 0.0) for o in OVERSUB}
            for m in MECHS_TO_RUN
        },
        "reproduction_mismatches_vs_design_space_json": rc,
    }
    (RESULTS / "design_space_epochfence.json").write_text(
        json.dumps(curated, indent=2))

    # console comparison table
    print(f"\nDesign-space epoch-fence rows (capacity, {len(SEEDS)} seeds, "
          f"pool={CAPACITY}, budget={BUDGET})")
    hdr = (f"{'oversub':>8} {'mechanism':>11} {'valid_thr':>9} {'RPE%':>7} "
           f"{'pin_pk':>7} {'reclaim%':>9}")
    print(hdr)
    for o in OVERSUB:
        for m in MECHS_TO_RUN:
            a = agg[(m, o)]
            thr = (a["valid_throughput_Bpns"] / prose_base[o]
                   if prose_base[o] > 0 else 0.0)
            print(f"{o:>7}x {m:>11} {thr:>8.3f}x "
                  f"{a['rpe_payload_frac']*100:>6.2f}% "
                  f"{a['pinned_peak']:>7.0f} "
                  f"{a['reclaimable_capacity_frac']*100:>8.1f}%")

    # sanity: fence <= GENONLY at every point, PROSE == 0, fence nonzero
    for o in OVERSUB:
        ef = agg[("GENONLY_EF", o)]["rpe_payload_frac"]
        go = agg[("GENONLY", o)]["rpe_payload_frac"]
        pr = agg[("PROSE", o)]["rpe_payload_frac"]
        assert ef <= go + 1e-12, f"fence > GENONLY at {o}x"
        assert pr == 0.0, f"PROSE nonzero at {o}x"
        assert ef > 0.0, f"fence == 0 at {o}x — grace modeled too long"
    print("\nsanity: GENONLY_EF <= GENONLY at every point, PROSE == 0, "
          "GENONLY_EF > 0 — all hold")
    print(f"\nOutputs: {OUT / 'design_space_epochfence.csv'} and "
          f"{RESULTS / 'design_space_epochfence.json'}")
    return 0 if rc == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
