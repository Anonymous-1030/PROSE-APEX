#!/usr/bin/env python3
"""Design-space study — rebuttal to "REFCNT strawman" + reclaim-side checking.

Extends the Optimistic-Reclaim sweep (Table IV) with three additive mechanisms
from `experiments/oversub_reclaim.py` and classifies every mechanism on the two
axes that actually determine the outcome:

  axis 1 — PROTECTION TIMING:  pre-enqueue (protect before/at enqueue, hold
           across the queue wait) vs commit-time (validate-and-hold at
           admission, hold only across the transfer).
  axis 2 — TRANSFER-SET SELECTION AUTHORITY:  who picks the admitted set —
           the host (all candidates, or a pre-selected budget-sized subset)
           or the endpoint.

Findings this driver quantifies (no parameters tuned; raw engine output):

  * REFCNT (pre-enqueue, host/all-candidates) pins the whole offered backlog,
    exhausts the 512-entry pool, and cliffs (valid throughput 0.50x of PROSE at
    >=16x oversubscription, reclaimable capacity ~0).
  * REFCNT_S (pre-enqueue, host/budget-subset) makes the cliff DISAPPEAR — pool
    occupancy ~= the 32-entry admit budget at any oversubscription — but only
    because its identity-only pin acquisition skips the generation check: a
    reclaim+reincarnation in the snapshot->acquire window attaches the pin to
    the wrong incarnation, so ~17% of admits are stale (and ~0.17 re-select
    retries per descriptor). Cheap REFCNT is UNSAFE REFCNT.
  * RECLAIM_DEFER (pre-enqueue, endpoint/reclaim-side) moves the same
    pre-enqueue protection to the reclaim path (queued descriptors defer
    reclaim of their slots): the cliff sits at EXACTLY the same oversubscription
    point as REFCNT, and with no admission re-check / transfer-span pin it also
    leaks stale payload. Checking on the reclaim side does not help.
  * PROSE_HOSTSEL (commit-time, host-selected subset) == PROSE on every metric
    with ZERO stale admits: once the check is at commit time, who selects the
    transfer set does not affect safety. Protection timing, not selection
    authority, is the safety axis.

Grid: oversub {2,4,8,16,32,64} x 16 tenants x 10 seeds, pool 512, admit budget
32, 200 steps, mechanisms {PROSE, REFCNT, REFCNT_S, 2PHASE, GENONLY,
RECLAIM_DEFER} (+PROSE_HOSTSEL for the quadrant table), bound modes capacity
and token_table (paired traces; only the bound label differs).

Outputs:
  experiments/out/design_space/design_space.csv      — full per-run grid
  experiments/out/design_space/design_space.json     — aggregated grid + 2x2 cells
  experiments/out/design_space/fig_design_space.pdf  — 2x2 design-space figure
  results/design_space.json                          — curated, paper-facing
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.oversub_reclaim import (           # noqa: E402
    OversubConfig, generate_oversub_trace, replay_oversub, MECHS,
)

OUT = ROOT / "experiments" / "out" / "design_space"
RESULTS = ROOT / "results"

# ── Sweep (Table-IV grid + the two low-oversub points) ───────────────────────
OVERSUB = [2, 4, 8, 16, 32, 64]
TENANTS = 16
SEEDS = list(range(10))
BUDGET = 32
CAPACITY = 512
N_STEPS = 200
BOUND_MODES = ["capacity", "token_table"]

GRID_MECHS = ["PROSE", "REFCNT", "REFCNT_S", "2PHASE", "GENONLY", "RECLAIM_DEFER"]

# ── 2x2 design-space classification (evaluated at 32x) ───────────────────────
# (protection timing, selection authority) -> mechanism occupying that cell.
# REFCNT and REFCNT_S share the (pre-enqueue, host) cell as the all-candidates
# and budget-subset variants; RECLAIM_DEFER fills (pre-enqueue, endpoint).
CELL_MECHS = {
    ("pre-enqueue", "host"): ["REFCNT", "REFCNT_S"],
    ("pre-enqueue", "endpoint"): ["RECLAIM_DEFER"],
    ("commit-time", "host"): ["PROSE_HOSTSEL"],
    ("commit-time", "endpoint"): ["PROSE"],
}
CELL_VARIANT = {            # qualifier shown under the mechanism name
    "REFCNT": "all candidates pinned",
    "REFCNT_S": "budget subset pinned, no gen check",
    "RECLAIM_DEFER": "reclaim-side deferral",
    "PROSE_HOSTSEL": "host-selected subset, gen checked",
    "PROSE": "endpoint-selected, gen checked",
}

METRICS = ("valid_throughput_Bpns", "admission_p99_ns", "pinned_peak",
           "reclaimable_capacity_frac", "rpe_payload_frac", "stale_admit_rate",
           "retries_per_descriptor", "stale_admits", "retries",
           "evict_fired", "evict_blocked")


def run_grid(bound_mode: str) -> List[Dict]:
    rows: List[Dict] = []
    for oversub in OVERSUB:
        for seed in SEEDS:
            cfg = OversubConfig(
                oversubscription=oversub, n_tenants=TENANTS,
                admit_budget=BUDGET, n_steps=N_STEPS, capacity=CAPACITY,
                token_table=CAPACITY, bound_mode=bound_mode, seed=seed,
            )
            trace = generate_oversub_trace(cfg)
            for m in GRID_MECHS + ["PROSE_HOSTSEL"]:
                rows.append(replay_oversub(trace, MECHS[m]))
    return rows


def aggregate(rows: List[Dict]) -> Dict:
    """Mean over seeds, keyed by (bound_mode, mechanism, oversubscription)."""
    buckets: Dict = {}
    for r in rows:
        buckets.setdefault((r["bound_mode"], r["mechanism"],
                            r["oversubscription"]), []).append(r)
    agg: Dict = {}
    for k, rs in buckets.items():
        agg[k] = {kk: float(mean(r[kk] for r in rs)) for kk in METRICS}
        agg[k]["throughput_std"] = (
            float(pstdev([r["valid_throughput_Bpns"] for r in rs]))
            if len(rs) > 1 else 0.0)
    return agg


def norm_throughput(agg: Dict, bound: str, mech: str, oversub: int) -> float:
    base = agg[(bound, "PROSE", oversub)]["valid_throughput_Bpns"]
    v = agg[(bound, mech, oversub)]["valid_throughput_Bpns"]
    return v / base if base > 0 else 0.0


def quadrant_cells(agg: Dict, bound: str, oversub: int = 32) -> Dict:
    """The 2x2 (timing x authority) cells at `oversub`, PROSE-normalized."""
    cells: Dict = {}
    for (timing, authority), mechs in CELL_MECHS.items():
        entries = []
        for m in mechs:
            a = agg[(bound, m, oversub)]
            entries.append({
                "mechanism": m,
                "variant": CELL_VARIANT[m],
                "valid_thr_norm_to_prose": norm_throughput(agg, bound, m, oversub),
                "stale_admit_rate": a["stale_admit_rate"],
                "rpe_payload_frac": a["rpe_payload_frac"],
                "reclaimable_capacity_frac": a["reclaimable_capacity_frac"],
                "retries_per_descriptor": a["retries_per_descriptor"],
                "admission_p99_ns": a["admission_p99_ns"],
                "pinned_or_deferred_peak": a["pinned_peak"],
            })
        cells[f"{timing}|{authority}"] = entries
    return cells


def hypotheses(agg: Dict, bound: str) -> Dict:
    """Empirical verdicts on the revision claims (computed, never hardcoded)."""
    g = lambda m, o: agg[(bound, m, o)]
    return {
        "refcnt_s_cliff_disappears": {
            "refcnt_s_reclaimable_frac_64x": g("REFCNT_S", 64)["reclaimable_capacity_frac"],
            "refcnt_reclaimable_frac_64x": g("REFCNT", 64)["reclaimable_capacity_frac"],
            "refcnt_s_valid_thr_norm_64x": norm_throughput(agg, bound, "REFCNT_S", 64),
            "refcnt_valid_thr_norm_64x": norm_throughput(agg, bound, "REFCNT", 64),
            "holds": bool(g("REFCNT_S", 64)["reclaimable_capacity_frac"]
                          > g("REFCNT", 64)["reclaimable_capacity_frac"]),
        },
        "refcnt_s_trades_safety_for_it": {
            "stale_admit_rate_8x": g("REFCNT_S", 8)["stale_admit_rate"],
            "stale_admit_rate_32x": g("REFCNT_S", 32)["stale_admit_rate"],
            "stale_admit_rate_64x": g("REFCNT_S", 64)["stale_admit_rate"],
            "retries_per_descriptor_32x": g("REFCNT_S", 32)["retries_per_descriptor"],
            "holds": bool(g("REFCNT_S", 8)["stale_admit_rate"] > 0.0),
        },
        "reclaim_defer_cliff_coincides_with_refcnt": {
            "reclaimable_frac_by_oversub": {
                str(o): {"REFCNT": g("REFCNT", o)["reclaimable_capacity_frac"],
                         "RECLAIM_DEFER": g("RECLAIM_DEFER", o)["reclaimable_capacity_frac"]}
                for o in OVERSUB
            },
            "reclaim_defer_valid_thr_norm_64x": norm_throughput(agg, bound, "RECLAIM_DEFER", 64),
            "holds": bool(
                norm_throughput(agg, bound, "RECLAIM_DEFER", 64)
                <= 0.6 * norm_throughput(agg, bound, "PROSE", 64)
                and all(abs(g("RECLAIM_DEFER", o)["reclaimable_capacity_frac"]
                            - g("REFCNT", o)["reclaimable_capacity_frac"]) < 1e-9
                        for o in OVERSUB)),
        },
        "commit_time_check_safe_regardless_of_authority": {
            "prose_stale_admit_rate_32x": g("PROSE", 32)["stale_admit_rate"],
            "prose_hostsel_stale_admit_rate_32x": g("PROSE_HOSTSEL", 32)["stale_admit_rate"],
            "prose_rpe_payload_frac_32x": g("PROSE", 32)["rpe_payload_frac"],
            "prose_hostsel_rpe_payload_frac_32x": g("PROSE_HOSTSEL", 32)["rpe_payload_frac"],
            "holds": bool(g("PROSE", 32)["rpe_payload_frac"] == 0.0
                          and g("PROSE_HOSTSEL", 32)["rpe_payload_frac"] == 0.0
                          and g("PROSE", 32)["stale_admit_rate"] == 0.0
                          and g("PROSE_HOSTSEL", 32)["stale_admit_rate"] == 0.0),
        },
    }


def make_figure(agg: Dict, bound: str, oversub: int = 32) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    timings = ["pre-enqueue", "commit-time"]
    authorities = ["host", "endpoint"]
    cells = quadrant_cells(agg, bound, oversub)

    fig, axes = plt.subplots(2, 2, figsize=(11.5, 6.8))
    for i, timing in enumerate(timings):
        for j, authority in enumerate(authorities):
            ax = axes[i][j]
            ax.set_xticks([]); ax.set_yticks([])
            entries = cells[f"{timing}|{authority}"]
            # cell tint: green = full throughput AND zero stale; red otherwise
            safe = all(e["valid_thr_norm_to_prose"] > 0.95
                       and e["stale_admit_rate"] == 0.0
                       and e["rpe_payload_frac"] == 0.0 for e in entries)
            ax.set_facecolor("#e8f5e9" if safe else "#fdecea")
            for spine in ax.spines.values():
                spine.set_color("#2e7d32" if safe else "#c62828")
                spine.set_linewidth(2)
            lines = []
            for e in entries:
                lines.append(f"{e['mechanism']}")
                lines.append(f"({e['variant']})")
                lines.append(
                    f"valid thr {e['valid_thr_norm_to_prose']:.2f}x   "
                    f"stale-admit {e['stale_admit_rate']*100:.1f}%")
                lines.append(
                    f"RPE payload {e['rpe_payload_frac']*100:.1f}%   "
                    f"reclaimable {e['reclaimable_capacity_frac']*100:.1f}%")
                if e["retries_per_descriptor"] > 0:
                    lines.append(f"retries/desc {e['retries_per_descriptor']:.2f}")
                lines.append("")
            verdict = ("SAFE + NO CLIFF" if safe else
                       "CLIFF and/or STALE")
            lines.append(verdict)
            ax.text(0.5, 0.5, "\n".join(lines).strip(),
                    ha="center", va="center", fontsize=9.5, family="monospace",
                    transform=ax.transAxes)
            ax.set_title(f"{timing} protection / {authority} selects",
                         fontsize=10)

    fig.suptitle(
        f"Protection design space at {oversub}x oversubscription "
        f"({TENANTS} tenants, pool={CAPACITY}, budget={BUDGET}, "
        f"{len(SEEDS)} seeds, bound={bound})\n"
        f"protection timing (rows) x transfer-set selection authority (cols); "
        f"throughput normalized to PROSE",
        fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(OUT / f"fig_design_space.{ext}", dpi=150, bbox_inches="tight")
    plt.close(fig)


def write_outputs(all_rows: List[Dict], agg: Dict, modes: List[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    # raw per-run CSV
    cols = ["bound_mode", "mechanism", "oversubscription", "n_tenants", "seed",
            "valid_throughput_Bpns", "admission_p99_ns", "pinned_peak",
            "reclaimable_capacity_frac", "rpe_payload_frac", "stale_admits",
            "stale_admit_rate", "retries", "retries_per_descriptor",
            "evict_fired", "evict_blocked"]
    with open(OUT / "design_space.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    config = {"oversub": OVERSUB, "n_tenants": TENANTS, "seeds": SEEDS,
              "admit_budget": BUDGET, "pool": CAPACITY, "n_steps": N_STEPS,
              "bound_modes": BOUND_MODES, "mechanisms": GRID_MECHS + ["PROSE_HOSTSEL"]}

    grid_json = {"|".join(map(str, k)): v for k, v in agg.items()}
    cells = {bm: quadrant_cells(agg, bm, 32) for bm in modes}
    hypo = {bm: hypotheses(agg, bm) for bm in modes}
    (OUT / "design_space.json").write_text(json.dumps(
        {"config": config, "grid": grid_json,
         "design_space_2x2_32x": cells, "hypotheses": hypo}, indent=2))

    # curated, paper-facing: per-mechanism means (capacity) + 2x2 cells + verdicts
    curated = {
        "experiment": "design_space",
        "config": config,
        "table_iv_grid_capacity": {
            m: {str(o): agg[("capacity", m, o)] for o in OVERSUB}
            for m in GRID_MECHS + ["PROSE_HOSTSEL"]
        },
        "valid_thr_norm_to_prose_capacity": {
            m: {str(o): norm_throughput(agg, "capacity", m, o) for o in OVERSUB}
            for m in GRID_MECHS + ["PROSE_HOSTSEL"]
        },
        "design_space_2x2_32x_capacity": cells["capacity"],
        "hypotheses_capacity": hypo["capacity"],
    }
    (RESULTS / "design_space.json").write_text(json.dumps(curated, indent=2))

    # console summary at 2/8/32/64x (capacity bound)
    lines = ["Design-space study (16 tenants, pool=512, budget=32, "
             f"mean of {len(SEEDS)} seeds, bound=capacity)"]
    hdr = (f"{'oversub':>8} {'mechanism':>13} {'valid_thr':>9} {'stale_adm%':>10} "
           f"{'RPE%':>6} {'pin_pk':>7} {'reclaim%':>9} {'P99_ns':>9} {'retr/d':>6}")
    for oversub in (2, 8, 32, 64):
        lines.append(f"--- {oversub}x ---")
        lines.append(hdr)
        for m in GRID_MECHS + ["PROSE_HOSTSEL"]:
            a = agg[("capacity", m, oversub)]
            lines.append(
                f"{oversub:>7}x {m:>13} "
                f"{norm_throughput(agg, 'capacity', m, oversub):>8.2f}x "
                f"{a['stale_admit_rate']*100:>9.1f}% "
                f"{a['rpe_payload_frac']*100:>5.1f}% "
                f"{a['pinned_peak']:>7.0f} "
                f"{a['reclaimable_capacity_frac']*100:>8.1f}% "
                f"{a['admission_p99_ns']:>9.1f} "
                f"{a['retries_per_descriptor']:>6.2f}")
        lines.append("")
    txt = "\n".join(lines)
    (OUT / "design_space_summary.txt").write_text(txt + "\n")
    print(txt)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bound-mode", choices=BOUND_MODES + ["both"],
                    default="both")
    args = ap.parse_args()
    modes = BOUND_MODES if args.bound_mode == "both" else [args.bound_mode]

    all_rows: List[Dict] = []
    for bm in modes:
        print(f"[design-space] running grid for bound_mode={bm} ...")
        all_rows.extend(run_grid(bm))

    agg = aggregate(all_rows)
    write_outputs(all_rows, agg, modes)
    make_figure(agg, "capacity", 32)
    print(f"  wrote fig_design_space.pdf/png")
    print(f"\nOutputs in {OUT} and {RESULTS / 'design_space.json'}")


if __name__ == "__main__":
    main()
