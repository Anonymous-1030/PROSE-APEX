#!/usr/bin/env python3
"""Reject/retry cost study — what the commit-time gate COSTS per turned-away
descriptor, vs what the alternatives pay (paper rebuttal companion to
`run_design_space.py`).

The design-space study (`experiments/run_design_space.py`) establishes WHERE
each mechanism sits on protection-timing x selection-authority and the
resulting throughput/staleness outcomes. This driver quantifies the other side
of the ledger: the per-descriptor cost of saying "no" (or "not yet") under
each mechanism. Every number below is computed from the shared-trace replay
engine (`experiments/oversub_reclaim.py`, re-run deterministically here with
the design-space grid) or from the calibrated byte-level logs
(`results/baselines/raw/*.jsonl`); nothing is hand-entered.

WHAT IS MEASURED (per mechanism x oversubscription, bound_mode="capacity"):

  (a) admission-reject rate   — descriptors never admitted within their build
      step (the commit-time gate turns them away) / offered descriptors.
  (b) retry-count distribution per descriptor {0, 1, 2+} — only REFCNT_S
      retries (identity-only pin acquire fails on a simply-gone object and the
      host re-selects; each retry is one serialized 250 ns RTT on that
      descriptor's admission latency). Reconstructed per-descriptor from the
      shared trace's snapshot-window streams and cross-checked against the
      engine's aggregate `retries`/`stale_admits` counters.
  (c) mean added admission latency from retries (ns/descriptor).
  (d) throughput-loss decomposition of the offered payload bytes:

          offered payload = valid payload            (transferred, correct gen)
                          + stale payload (RPE)      (transferred, wrong gen)
                          + rejected payload         (never transferred)

      which sums EXACTLY (asserted in code), plus the two gate-cost terms that
      sit OUTSIDE the payload accounting:
          + reject-metadata overhead  = rejected descriptors * META_B (64 B;
            a PROSE reject is a metadata-only null completion — the rejected
            descriptor carries zero payload)
          + retry-latency opportunity cost = retries * 250 ns, expressed in
            payload-byte-equivalents at the calibrated link bandwidth.

BYTE COST OF A REJECT (from the raw byte-level logs): a rejected descriptor
under PROSE moves `descriptor_bytes + completion_bytes` = 128 B of control and
ZERO payload, while a stale issue under GenOnly/Unsafe (NoCheck) moves the
FULL payload including the stale bytes. Reported as the measured ratio and as
the repo convention META_B / CHUNK_PAYLOAD_B (simcxl_ext/cxl_admission_sim.py).

Outputs:
  results/reject_cost.json                       — structured per mechanism/oversub
  experiments/out/reject_cost/fig_reject_cost.pdf — decomposition + retry figure
  experiments/out/reject_cost/fig_reject_cost.png
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.oversub_reclaim import (           # noqa: E402
    OversubConfig, generate_oversub_trace, replay_oversub, MECHS,
)
from experiments.run_design_space import (          # noqa: E402
    OVERSUB, TENANTS, SEEDS, BUDGET, CAPACITY, N_STEPS, GRID_MECHS,
)
from simcxl_ext.cxl_admission_sim import CHUNK_PAYLOAD_B, META_B  # noqa: E402

OUT = ROOT / "experiments" / "out" / "reject_cost"
RESULTS = ROOT / "results"
RAW = RESULTS / "baselines" / "raw"

ALL_MECHS = GRID_MECHS + ["PROSE_HOSTSEL"]
# The five mechanisms the paper's main comparison rests on (figure panel a).
MAIN_MECHS = ["PROSE", "REFCNT", "REFCNT_S", "2PHASE", "GENONLY"]
FIG_OVERSUB = [8, 32, 64]
# Raw-log method names for the byte-cost comparison: the commit-time gate
# (PROSE) vs the unchecked/full-payload-stale designs.
RAW_STALE_METHODS = ["GenOnly", "NoCheck"]
RAW_REJECT_METHOD = "PROSE"

# One REFCNT_S re-select retry = one serialized round trip on that descriptor's
# admission latency (engine constant, same as REFCNT's serialized acquire).
RETRY_RTT_NS = MECHS["REFCNT_S"].serialized_acquire_ns


# ── Grid computation (deterministic re-run of the design-space grid) ─────────
def _cfg(oversub: int, seed: int) -> OversubConfig:
    return OversubConfig(oversubscription=oversub, n_tenants=TENANTS,
                         admit_budget=BUDGET, n_steps=N_STEPS,
                         capacity=CAPACITY, token_table=CAPACITY,
                         bound_mode="capacity", seed=seed)


def retry_counts_per_descriptor(trace, mech_name: str, n_admitted: int) -> np.ndarray:
    """Per-ADMITTED-descriptor retry counts, reconstructed from the shared trace.

    Only REFCNT_S retries: a candidate is admitted iff it is within the first
    `admit_budget` of its build step (the engine admits in build order and the
    budget-sized pin set is never pool-constrained), and it retries iff its
    object was reclaimed in the snapshot->acquire window (`race_snap`) without
    reincarnation (`~reincarn`) — the acquire then fails and the host
    re-selects once. The engine's model allows at most one retry per
    descriptor, so the count array is 0/1; the 2+ bucket is reported (and is
    zero) for completeness.
    """
    if mech_name != "REFCNT_S":
        return np.zeros(n_admitted, dtype=np.int64)
    cfg = trace.cfg
    per_step = cfg.oversubscription * cfg.admit_budget
    idx = np.arange(trace.n_requests)
    admitted_mask = (idx % per_step) < cfg.admit_budget
    retry_mask = trace.race_snap & ~trace.reincarn & admitted_mask
    counts = retry_mask[admitted_mask].astype(np.int64)
    assert len(counts) == n_admitted
    return counts


def compute_cell(mech_name: str, oversub: int, seeds: List[int]) -> Dict:
    """All reject-cost metrics for one (mechanism, oversubscription) cell,
    pooled over `seeds`. Pure computation: replays the shared traces."""
    mech = MECHS[mech_name]
    obj_bytes = float(_cfg(oversub, seeds[0]).base.object_bytes)
    link_B_per_ns = float(_cfg(oversub, seeds[0]).base.link_bw_gbps)  # GB/s == B/ns

    offered = admitted = rejected = 0
    valid_bytes = stale_bytes = requested_bytes = 0.0
    retries_total = stale_admits_total = 0
    retry_count_pool: List[np.ndarray] = []
    thr_seeds: List[float] = []

    for seed in seeds:
        trace = generate_oversub_trace(_cfg(oversub, seed))
        r = replay_oversub(trace, mech)
        # admitted descriptors: exact — valid+stale payload is always a whole
        # number of objects (stale halves cancel against the valid side).
        n_ad = (r["valid_bytes"] + r["stale_bytes"]) / obj_bytes
        assert abs(n_ad - round(n_ad)) < 1e-6, (mech_name, oversub, seed, n_ad)
        n_ad = int(round(n_ad))

        counts = retry_counts_per_descriptor(trace, mech_name, n_ad)
        # Cross-check the reconstruction against the engine's own aggregates.
        assert int(counts.sum()) == r["retries"], (mech_name, oversub, seed)
        if mech_name != "REFCNT_S":
            assert r["retries"] == 0 and r["stale_admits"] == 0

        offered += r["n_requests"]
        admitted += n_ad
        valid_bytes += r["valid_bytes"]
        stale_bytes += r["stale_bytes"]
        requested_bytes += r["requested_bytes"]
        retries_total += r["retries"]
        stale_admits_total += r["stale_admits"]
        retry_count_pool.append(counts)
        thr_seeds.append(r["valid_throughput_Bpns"])

    rejected = offered - admitted
    rejected_payload_bytes = rejected * obj_bytes
    # (d) payload accounting identity — must hold exactly.
    assert abs(valid_bytes + stale_bytes + rejected_payload_bytes
               - requested_bytes) < 1e-3, (mech_name, oversub)

    reject_metadata_bytes = rejected * META_B
    retry_ns_total = retries_total * RETRY_RTT_NS
    # retry-latency opportunity cost in payload-byte-equivalents: the payload
    # the calibrated link could have moved during the serialized retry RTTs.
    retry_opportunity_bytes = retry_ns_total * link_B_per_ns

    counts = (np.concatenate(retry_count_pool) if retry_count_pool
              else np.zeros(0, dtype=np.int64))
    n0 = int((counts == 0).sum())
    n1 = int((counts == 1).sum())
    n2p = int((counts >= 2).sum())

    denom = float(requested_bytes)
    fracs = {
        "valid_frac": valid_bytes / denom,
        "stale_rpe_frac": stale_bytes / denom,
        "rejected_payload_frac": rejected_payload_bytes / denom,
        # gate-cost terms (outside the payload identity; tiny by construction)
        "reject_metadata_frac": reject_metadata_bytes / denom,
        "retry_opportunity_frac": retry_opportunity_bytes / denom,
    }
    # Accounting check: the payload decomposition sums to exactly 1.
    assert abs(fracs["valid_frac"] + fracs["stale_rpe_frac"]
               + fracs["rejected_payload_frac"] - 1.0) < 1e-12

    return {
        "mechanism": mech_name,
        "oversubscription": oversub,
        "n_seeds": len(seeds),
        "offered_descriptors": offered,
        "admitted_descriptors": admitted,
        "rejected_descriptors": rejected,
        # (a) admission-reject rate
        "reject_rate": rejected / offered if offered else 0.0,
        "stale_admits": stale_admits_total,
        "stale_admit_rate": (stale_admits_total / admitted if admitted else 0.0),
        # (b) retry-count distribution per admitted descriptor
        "retry_dist": {"0": n0, "1": n1, "2+": n2p},
        "retry_dist_frac": {
            "0": n0 / admitted if admitted else 0.0,
            "1": n1 / admitted if admitted else 0.0,
            "2+": n2p / admitted if admitted else 0.0,
        },
        "retries": retries_total,
        "retries_per_descriptor": (retries_total / admitted if admitted else 0.0),
        # (c) mean added admission latency from retries (ns per admitted desc)
        "retry_added_ns_per_descriptor": (
            retries_total * RETRY_RTT_NS / admitted if admitted else 0.0),
        # (d) throughput-loss decomposition (fractions of offered payload)
        "offered_payload_bytes": requested_bytes,
        "valid_payload_bytes": valid_bytes,
        "stale_payload_bytes": stale_bytes,
        "rejected_payload_bytes": rejected_payload_bytes,
        "reject_metadata_bytes": reject_metadata_bytes,
        "retry_opportunity_bytes_equiv": retry_opportunity_bytes,
        "decomposition": fracs,
        "decomposition_sums_to_one": True,   # asserted above
        "valid_throughput_Bpns_mean": float(np.mean(thr_seeds)),
        # resulting valid-throughput normalization: valid / offered payload
        "valid_throughput_normalization": fracs["valid_frac"],
    }


def build_grid(seeds: List[int]) -> Dict:
    grid: Dict = {}
    for mech in ALL_MECHS:
        grid[mech] = {str(o): compute_cell(mech, o, seeds) for o in OVERSUB}
    # valid throughput normalized to PROSE (same oversub, same seeds)
    for mech in ALL_MECHS:
        for o in OVERSUB:
            base = grid["PROSE"][str(o)]["valid_throughput_Bpns_mean"]
            v = grid[mech][str(o)]["valid_throughput_Bpns_mean"]
            grid[mech][str(o)]["valid_thr_norm_to_prose"] = v / base if base else 0.0
    return grid


# ── Raw byte-level logs: cost of a reject vs a full-payload stale issue ──────
def analyze_raw_logs() -> Dict:
    """Per-descriptor byte cost of a reject (metadata only) vs a stale issue
    (full payload), aggregated over every raw run of the relevant methods."""
    per_method: Dict = {}
    for path in sorted(glob.glob(str(RAW / "*.jsonl"))):
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                m = per_method.setdefault(r["method"], {
                    "n_requests": 0,
                    "rejects": 0, "reject_payload_bytes": 0,
                    "reject_control_bytes": 0, "reject_stale_bytes": 0,
                    "rpe_events": 0, "rpe_wire_payload_bytes": 0,
                    "rpe_stale_payload_bytes": 0,
                })
                m["n_requests"] += 1
                if r["reject_ns"] is not None:
                    m["rejects"] += 1
                    m["reject_payload_bytes"] += r["wire_payload_bytes"]
                    m["reject_control_bytes"] += r["control_bytes"]
                    m["reject_stale_bytes"] += r["stale_payload_bytes"]
                if r["rpe_event"]:
                    m["rpe_events"] += 1
                    m["rpe_wire_payload_bytes"] += r["wire_payload_bytes"]
                    m["rpe_stale_payload_bytes"] += r["stale_payload_bytes"]

    rej = per_method[RAW_REJECT_METHOD]
    assert rej["rejects"] > 0, "no PROSE rejects found in raw logs"
    reject_meta_per_desc = rej["reject_control_bytes"] / rej["rejects"]
    reject_payload_per_desc = rej["reject_payload_bytes"] / rej["rejects"]

    stale_methods = {}
    for meth in RAW_STALE_METHODS:
        s = per_method[meth]
        assert s["rpe_events"] > 0, f"no {meth} RPE events in raw logs"
        stale_methods[meth] = {
            "rpe_events": s["rpe_events"],
            "payload_bytes_per_stale_descriptor":
                s["rpe_wire_payload_bytes"] / s["rpe_events"],
            "stale_bytes_per_stale_descriptor":
                s["rpe_stale_payload_bytes"] / s["rpe_events"],
        }
    mean_stale_payload = float(np.mean(
        [v["payload_bytes_per_stale_descriptor"] for v in stale_methods.values()]))

    return {
        "source": "results/baselines/raw/*.jsonl (all workloads, all seeds)",
        "reject_under_prose": {
            "rejects": rej["rejects"],
            "payload_bytes_per_reject": reject_payload_per_desc,
            "control_bytes_per_reject": reject_meta_per_desc,
            "note": ("reject = BDB descriptor + null completion, zero payload "
                     "issued; control = descriptor_bytes + completion_bytes"),
        },
        "stale_issue_under": stale_methods,
        "byte_ratio": {
            # measured: control bytes per PROSE reject vs full wire payload per
            # stale descriptor under GenOnly/Unsafe
            "measured_control_per_reject_over_payload_per_stale":
                reject_meta_per_desc / mean_stale_payload,
            # repo convention: 64 B metadata summary vs 64 KB chunk payload
            "convention_META_B_over_CHUNK_PAYLOAD_B": META_B / CHUNK_PAYLOAD_B,
            "META_B": META_B,
            "CHUNK_PAYLOAD_B": CHUNK_PAYLOAD_B,
        },
        "per_method_raw": per_method,
    }


# ── Figure ────────────────────────────────────────────────────────────────────
def make_figure(grid: Dict, raw: Dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (axa, axb, axc) = plt.subplots(
        1, 3, figsize=(14.5, 4.6), gridspec_kw={"width_ratios": [2.6, 1, 1]})

    # (a) throughput-loss decomposition of offered payload, stacked
    c_valid, c_stale, c_rej = "#2e7d32", "#c62828", "#bdbdbd"
    short = {"PROSE": "PROSE", "REFCNT": "REFCNT", "REFCNT_S": "REF_S",
             "2PHASE": "2PHASE", "GENONLY": "GENONLY"}
    xticks, xlabels = [], []
    x = 0.0
    meta_ppm_max = 0.0
    for gi, o in enumerate(FIG_OVERSUB):
        for m in MAIN_MECHS:
            d = grid[m][str(o)]["decomposition"]
            meta_ppm_max = max(meta_ppm_max, d["reject_metadata_frac"] * 1e6)
            axa.bar(x, d["valid_frac"], 0.85, color=c_valid, edgecolor="none")
            axa.bar(x, d["stale_rpe_frac"], 0.85, bottom=d["valid_frac"],
                    color=c_stale, edgecolor="none")
            axa.bar(x, d["rejected_payload_frac"], 0.85,
                    bottom=d["valid_frac"] + d["stale_rpe_frac"],
                    color=c_rej, edgecolor="none")
            # retry tax is a REFCNT_S-only term — annotate just those bars
            retry_ns = grid[m][str(o)]["retry_added_ns_per_descriptor"]
            if retry_ns > 0:
                axa.text(x, 1.02, f"+{retry_ns:.0f} ns/d\nretry",
                         ha="center", va="bottom", fontsize=6.5,
                         color="#ef6c00")
            xticks.append(x)
            xlabels.append(short[m])
            x += 1.0
        x += 0.8                      # gap between oversub groups
    axa.set_xticks(xticks)
    axa.set_xticklabels(xlabels, fontsize=7, rotation=45, ha="right")
    axa.set_ylim(0, 1.16)
    axa.set_ylabel("fraction of offered payload bytes")
    axa.set_title("(a) offered-payload decomposition at 8/32/64x "
                  "(5 main mechanisms)\nreject-metadata overhead <= "
                  f"{meta_ppm_max:.0f} ppm of offered payload at every point "
                  "(too small to see)", fontsize=9)
    for gi, o in enumerate(FIG_OVERSUB):
        center = gi * (len(MAIN_MECHS) + 0.8) + (len(MAIN_MECHS) - 1) / 2
        axa.text(center, -0.38, f"{o}x", ha="center", va="top",
                 fontsize=10, fontweight="bold",
                 transform=axa.get_xaxis_transform())
    from matplotlib.patches import Patch
    fig.legend(handles=[
        Patch(color=c_valid, label="valid payload"),
        Patch(color=c_stale, label="stale payload (RPE)"),
        Patch(color=c_rej, label="rejected payload (never transferred)"),
    ], fontsize=8, loc="upper left", ncol=3, bbox_to_anchor=(0.06, 0.90),
        frameon=False)

    # (b) REFCNT_S retry-count distribution per admitted descriptor
    rs = grid["REFCNT_S"]
    xs = np.arange(len(FIG_OVERSUB))
    p1 = [rs[str(o)]["retry_dist_frac"]["1"] for o in FIG_OVERSUB]
    p0 = [rs[str(o)]["retry_dist_frac"]["0"] for o in FIG_OVERSUB]
    axb.bar(xs, p0, 0.6, color="#bdbdbd")
    axb.bar(xs, p1, 0.6, bottom=p0, color="#ef6c00")
    for i, o in enumerate(FIG_OVERSUB):
        added = rs[str(o)]["retry_added_ns_per_descriptor"]
        axb.text(i, 1.02, f"+{added:.1f} ns/desc", ha="center", fontsize=7.5)
    axb.set_xticks(xs)
    axb.set_xticklabels([f"{o}x" for o in FIG_OVERSUB])
    axb.set_ylim(0, 1.15)
    axb.set_ylabel("fraction of admitted descriptors")
    axb.set_title("(b) REFCNT_S retry distribution\n"
                  "(gray = 0, orange = 1 retry; 2+ = 0)", fontsize=9)

    # (c) byte cost of one reject vs one full-payload stale issue (log scale)
    names = ["PROSE reject\n(metadata only)", "GenOnly stale\n(full payload)",
             "Unsafe stale\n(full payload)"]
    stale_keys = RAW_STALE_METHODS          # GenOnly, NoCheck(=Unsafe)
    vals = [raw["reject_under_prose"]["control_bytes_per_reject"]]
    vals += [raw["stale_issue_under"][m]["payload_bytes_per_stale_descriptor"]
             for m in stale_keys]
    colors = ["#2e7d32", "#c62828", "#c62828"]
    bars = axc.bar(range(len(vals)), vals, 0.6, color=colors)
    for b, v in zip(bars, vals):
        axc.text(b.get_x() + b.get_width() / 2, v * 1.15, f"{v:,.0f} B",
                 ha="center", fontsize=8)
    ratio = raw["byte_ratio"]["measured_control_per_reject_over_payload_per_stale"]
    axc.set_yscale("log")
    axc.set_ylim(10, 5e5)
    axc.set_xticks(range(len(names)))
    axc.set_xticklabels(names, fontsize=7.5)
    axc.set_ylabel("bytes on wire per descriptor (log)")
    axc.set_title(f"(c) reject vs stale-issue byte cost\n"
                  f"(measured ratio {ratio:.2e}; "
                  f"META_B/CHUNK_PAYLOAD_B = 1/{CHUNK_PAYLOAD_B // META_B})",
                  fontsize=9)

    fig.suptitle(
        "Cost of the commit-time gate: rejected descriptors pay metadata only; "
        "retries are a REFCNT_S-only tax; stale payload is the unsafe designs' tax",
        fontsize=10.5)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(OUT / f"fig_reject_cost.{ext}", dpi=150, bbox_inches="tight")
    plt.close(fig)


# ── Console table ─────────────────────────────────────────────────────────────
def format_table(grid: Dict) -> str:
    lines = ["Reject/retry cost study (16 tenants, pool=512, budget=32, "
             f"seeds={len(SEEDS)}, bound=capacity)",
             "decomposition = fractions of offered payload bytes "
             "(valid + stale + rejected-payload = 1 exactly)"]
    hdr = (f"{'oversub':>8} {'mechanism':>13} {'rej_rate':>8} {'retr/d':>7} "
           f"{'+ns/desc':>9} {'valid':>7} {'stale':>7} {'rej_payl':>8} "
           f"{'meta_ppm':>9} {'thr_norm':>8}")
    for o in FIG_OVERSUB:
        lines.append(f"--- {o}x ---")
        lines.append(hdr)
        for m in ALL_MECHS:
            c = grid[m][str(o)]
            d = c["decomposition"]
            lines.append(
                f"{o:>7}x {m:>13} {c['reject_rate']:>8.4f} "
                f"{c['retries_per_descriptor']:>7.3f} "
                f"{c['retry_added_ns_per_descriptor']:>9.2f} "
                f"{d['valid_frac']:>7.4f} {d['stale_rpe_frac']:>7.4f} "
                f"{d['rejected_payload_frac']:>8.4f} "
                f"{d['reject_metadata_frac'] * 1e6:>9.1f} "
                f"{c['valid_thr_norm_to_prose']:>7.2f}x")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    print("[reject-cost] replaying design-space grid (capacity) ...")
    grid = build_grid(SEEDS)
    print("[reject-cost] analyzing raw byte-level logs ...")
    raw = analyze_raw_logs()

    report = {
        "experiment": "reject_cost",
        "config": {
            "oversub": OVERSUB, "n_tenants": TENANTS, "seeds": SEEDS,
            "admit_budget": BUDGET, "pool": CAPACITY, "n_steps": N_STEPS,
            "bound_mode": "capacity", "mechanisms": ALL_MECHS,
            "META_B": META_B, "CHUNK_PAYLOAD_B": CHUNK_PAYLOAD_B,
            "retry_rtt_ns": RETRY_RTT_NS,
        },
        "per_mechanism": grid,
        "byte_cost_raw_logs": raw,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "reject_cost.json").write_text(json.dumps(report, indent=2))

    make_figure(grid, raw)
    print(format_table(grid))
    br = raw["byte_ratio"]
    print("byte ratio (reject vs stale issue): "
          f"measured {br['measured_control_per_reject_over_payload_per_stale']:.3e}, "
          f"convention META_B/CHUNK_PAYLOAD_B "
          f"{br['convention_META_B_over_CHUNK_PAYLOAD_B']:.3e}")
    print(f"\nOutputs: {RESULTS / 'reject_cost.json'}, "
          f"{OUT / 'fig_reject_cost.pdf'} (+.png)")


if __name__ == "__main__":
    main()
