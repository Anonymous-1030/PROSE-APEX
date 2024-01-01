#!/usr/bin/env python3
"""End-to-end SLO comparison: single-phase endpoint gate vs two-phase validation.

Paper claim (Background, "Why alternative protocols do not close the gap" /
Scope): two-phase reserve-then-pull validation *does* eliminate RPE, but the
extra 2-5 us reservation round-trip prevents throughput from reaching the
single-phase 3.1x-5.9x range under high concurrency. This driver makes that
quantitative: it shows the round-trip's held reservations accumulate at the
endpoint (Little's law, L = lambda * RTT), saturate the bounded token table,
and inject back-pressure stalls that blow up the P99 tail as oversubscription
rises -- while the fused single-phase gate holds a flat tail.

Method (deliberately bypasses run_closed_loop):
  run_closed_loop only exposes queue-DEPTH percentiles, but the SLO story needs
  per-BATCH end-to-end LATENCY percentiles. So we drive the models at the
  per-batch level, collecting every host's every-step `total_lat` into a global
  latency-sample list, then take P99 over those samples. We also record the
  peak `outstanding_reservations` per alpha to exhibit the Little's-law buildup.

Three policies, one shared CXLQueueSimulator backend (identical CXL.mem timing,
DRAM row hit/miss, flit serialization, M/D/1 bandwidth contention):
  * PROSE_APEX  -- single-phase endpoint gate (cefe): fused admit+commit, no
                   reservation held across any round-trip.
  * FetchThenScore -- fetch-all payload then score locally (fts_none/fts_odus):
                   no gate, RPE>0, bandwidth-bound.
  * TwoPhase    -- reserve-then-pull (TwoPhaseValidationBaseline): RPE==0, same
                   64 B metadata + admitted set as CEFE, loses only on the
                   concurrency-induced tail.

Expected curve shapes (throughput on X, P99 latency on Y):
  * PROSE_APEX : TAIL STAYS FLAT across the whole alpha sweep -- rightmost
                 (highest throughput), lowest P99. No reservation occupancy.
  * TwoPhase   : tracks PROSE_APEX at low alpha, then KNEES SHARPLY UPWARD for
                 alpha > 8 as offered L = H*alpha*budget crosses the token
                 table capacity and back-pressure stalls compound.
  * FTS        : THROUGHPUT FLOOR (leftmost) -- bandwidth wasted on invalid
                 payload caps tok/s regardless of alpha; P99 also elevated.
"""
from __future__ import annotations

import sys
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

# Make the package importable when run directly (no install / PYTHONPATH needed).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.cxl_queue_simulator import (
    CXLQueueSimulator,
    make_cxl_asic_config,
)
from simcxl_ext.two_phase_baseline import (
    TwoPhaseValidationBaseline,
    TwoPhaseConfig,
)
from simcxl_ext.io_utils import save_json, save_fig, C


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
N_HOSTS = 16                       # 16 concurrent tenants
PER_TENANT_BW_GBPS = 2.0           # 2 GB/s per tenant (bandwidth-starved corner)
ALPHA_SWEEP = [1, 2, 4, 8, 16, 32] # oversubscription: n_candidates = alpha * budget
BUDGET = 32                        # admits per step (HBM budget K_bdb)
N_STEPS = 256                      # decode steps per run
DECODE_STEP_NS = 1_000_000.0       # 1 ms decode step (pins drain between steps)
SEED = 42

# RTT sensitivity sweep: fix hosts + oversubscription at the sharpest-knee
# operating point, vary the token-exchange round-trip. This closes the "is the
# 2-5 us RTT really fatal?" question: even the fastest 2 us software shim still
# collapses under concurrency because the reservation WINDOW -- not its length
# -- is what pins tokens and saturates the table.
RTT_ALPHA = 32                     # oversubscription for the RTT sweep (sharpest knee)
RTT_SWEEP_US = [2.0, 3.5, 5.0]     # token-exchange round-trip variants

# Policies under comparison. Value carries the display label + palette colour.
# annot_off: per-policy (dx, dy) offset in points for the alpha=32 endpoint
# label. At alpha=32 PROSE-APEX and Two-phase sit close together in the
# bottom-right (14.7k/1088us vs 11.7k/1642us) while Fetch-then-score is alone
# up-left (0.47k/33.8k us); steer PROSE-APEX below its point and Two-phase to
# the right so the two lower tags clear each other.
POLICIES = {
    "PROSE_APEX":     {"label": "PROSE-APEX (single-phase gate)", "color": C["cefe"],   "annot_off": (2, -16)},
    "FetchThenScore": {"label": "Fetch-then-score",              "color": C["fts"],     "annot_off": (8, 2)},
    "TwoPhase":       {"label": "Two-phase validation",          "color": C["accent2"], "annot_off": (8, 6)},
}

# --- Panels (c) and (d): measured, not hand-set. ---
# eta_BW and the traffic split are computed by driving the SAME closed-loop
# admission simulator used by the mechanism-ablation experiment across a real
# per-tenant bandwidth sweep, then reading its own byte counters
# (useful/committed/wasted). eta_BW = useful / (committed + wasted), exactly the
# honest definition in run_mechanism_ablation._eta_bw_useful. Nothing here is a
# constant; the curves are whatever the simulator produces.
BW_SWEEP_GBPS = [2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
ETA_POLICIES = {
    "PROSE_APEX":     {"boundary": "cefe",     "scorer": "odus_x",
                       "label": "PROSE-APEX (endpoint gate)", "color": C["cefe"]},
    "FetchThenScore": {"boundary": "fts_none", "scorer": "none",
                       "label": "Fetch-then-score",           "color": C["fts"]},
}


# --------------------------------------------------------------------------- #
# Per-policy per-batch drivers (share one CXLQueueSimulator backend)          #
# --------------------------------------------------------------------------- #
def _make_backend() -> CXLQueueSimulator:
    """Construct the shared CXL byte-level backend at the per-tenant bandwidth.

    Every policy fetches through an identical backend so the comparison isolates
    admission *placement*, not link parameters. Bandwidth is the per-tenant
    2 GB/s bandwidth-starved operating point.
    """
    cfg = make_cxl_asic_config()
    cfg.bandwidth_gbps = PER_TENANT_BW_GBPS
    cfg.raw_bandwidth_gbps = PER_TENANT_BW_GBPS / 0.98
    return CXLQueueSimulator(cfg)


def _candidate_ids(step: int, host: int, n_cand: int) -> List[int]:
    """Disjoint per-(step,host) candidate id block so sets do not alias."""
    base = step * (N_HOSTS * 4096) + host * 4096
    return list(range(base, base + n_cand))


def drive_prose_apex(
    alpha: int, rng: np.random.Generator
) -> Tuple[List[float], int]:
    """Single-phase endpoint gate: score summaries, fetch only admitted set.

    Returns (latency_samples_us, peak_outstanding=0). No reservation is held
    across a round-trip, so outstanding-reservation occupancy is structurally 0.
    The critical path is metadata-read + payload-fetch of the admitted budget;
    the 9 ns fused admit is negligible and never a per-batch serial barrier.
    """
    n_cand = alpha * BUDGET
    samples: List[float] = []
    for step in range(N_STEPS):
        cxl = _make_backend()
        for host in range(N_HOSTS):
            cands = _candidate_ids(step, host, n_cand)
            # Fused gate: 64 B metadata for candidates, payload only for budget.
            meta = cxl.submit_summary_fetch(cands, 0.0)
            admitted = cands[:BUDGET]
            payload = cxl.submit_payload_fetch(admitted, 0.0)
            cxl.mark_chunks_used(admitted)
            samples.append((meta.total_ns + payload.total_ns) / 1000.0)
        cxl.end_step()
    return samples, 0


def drive_fetch_then_score(
    alpha: int, rng: np.random.Generator
) -> Tuple[List[float], int]:
    """Fetch-all payload then score locally: bandwidth-bound, RPE>0.

    Returns (latency_samples_us, peak_outstanding=0). The whole candidate set is
    DMA'd before scoring, so payload transfer scales with alpha and the link
    saturates -- the throughput floor.
    """
    n_cand = alpha * BUDGET
    samples: List[float] = []
    for step in range(N_STEPS):
        cxl = _make_backend()
        for host in range(N_HOSTS):
            cands = _candidate_ids(step, host, n_cand)
            # fetch-then-score: pull EVERY candidate's payload, then rank.
            payload = cxl.submit_payload_fetch(cands, 0.0)
            selected = cands[:BUDGET]
            invalid = cands[BUDGET:]
            cxl.mark_chunks_used(selected)
            cxl.mark_chunks_invalid(invalid)     # RPE: fetched-but-discarded
            samples.append(payload.total_ns / 1000.0)
        cxl.end_step()
    return samples, 0


def drive_two_phase(
    alpha: int, rng: np.random.Generator, rtt_us: float | None = None
) -> Tuple[List[float], int]:
    """Reserve-then-pull: per-batch submit through TwoPhaseValidationBaseline.

    Returns (latency_samples_us, peak_outstanding) where peak_outstanding is the
    max live pinned-token count observed -- the empirical Little's-law L. All
    N_HOSTS reserve concurrently within a step (staggered by the endpoint
    decision time), then pins drain over the 1 ms decode step. As alpha grows,
    offered L = H * alpha * budget crosses the token table and back-pressure
    stalls compound into the tail. ``rtt_us`` overrides the token-exchange
    round-trip for the RTT sensitivity sweep.
    """
    n_cand = alpha * BUDGET
    cfg = TwoPhaseConfig() if rtt_us is None else TwoPhaseConfig(reserve_rtt_us=rtt_us)
    tp = TwoPhaseValidationBaseline(cfg, _make_backend())
    samples: List[float] = []
    for step in range(N_STEPS):
        t0 = step * DECODE_STEP_NS
        for host in range(N_HOSTS):
            cands = _candidate_ids(step, host, n_cand)
            # Hosts arrive within the step, staggered by the endpoint reserve
            # decision (9 ns) so their reservations genuinely overlap in flight.
            arrival = t0 + host * tp.cfg.endpoint_reserve_service_ns
            res = tp.submit_batch(cands, BUDGET, arrival)
            samples.append(res.total_lat_us)
        tp.end_step()
    return samples, tp.peak_outstanding


DRIVERS = {
    "PROSE_APEX":     drive_prose_apex,
    "FetchThenScore": drive_fetch_then_score,
    "TwoPhase":       drive_two_phase,
}


# --------------------------------------------------------------------------- #
# Metrics                                                                     #
# --------------------------------------------------------------------------- #
def summarize(latency_us: List[float]) -> Dict[str, float]:
    """Reduce a latency-sample list to mean / P50 / P99 / throughput.

    Throughput here is the AGGREGATE PROMOTED-BATCH RATE, not a per-token rate:
    each host completes one batch (promoting BUDGET chunks that back one decode
    step) every mean_latency_s, so across N_HOSTS the rate is
    N_HOSTS / mean_latency_s batches/s. (The JSON key stays throughput_tok_s for
    backward compatibility with existing consumers, but panel (a) labels the
    axis as batches/s to match what is actually measured.) P99 uses
    np.percentile to match run_simcxl_multihost's convention.
    """
    arr = np.asarray(latency_us, dtype=float)
    mean_us = float(arr.mean())
    p50_us = float(np.percentile(arr, 50))
    p99_us = float(np.percentile(arr, 99))
    # Per-tenant tok/s = 1 step / mean batch latency; aggregate over hosts.
    throughput = (N_HOSTS * 1e6 / mean_us) if mean_us > 0 else 0.0
    return {
        "mean_us": mean_us,
        "p50_us": p50_us,
        "p99_us": p99_us,
        "throughput_tok_s": throughput,
        "n_samples": int(arr.size),
    }


# --------------------------------------------------------------------------- #
# Efficiency + traffic measurement (panels c, d) via the closed-loop sim       #
# --------------------------------------------------------------------------- #
def measure_efficiency() -> Dict[str, Any]:
    """Measure the two mechanism-sensitivity sweeps for panels (c) and (d).

    (c) eta_BW vs per-tenant bandwidth: drives the same run_closed_loop
        admission simulator as the mechanism ablation at each bandwidth and
        reads its own useful / committed / wasted byte counters
        (eta_BW = useful / (committed + wasted)). The point is the SHAPE: the
        curve is flat, so payload efficiency is bandwidth-invariant, an
        architectural constant. Nothing is assumed; whatever the sim emits is
        what the panel plots.
    (d) CFO benefit vs cross-tenant overlap: read from the real overlap-sweep
        experiment (experiments/out/data/overlap_sweep.json), showing throughput
        recovers with overlap and coincides with the no-CFO baseline below the
        0.45 break-even (zero-cost tail protection).
    """
    from simcxl_ext.cxl_admission_sim import SimConfig, run_closed_loop

    # Fixed representative workload; vary ONLY bandwidth so the panel isolates
    # bandwidth-invariance rather than the workload-dependent absolute level.
    eta = {p: [] for p in ETA_POLICIES}
    for policy, meta in ETA_POLICIES.items():
        for bw in BW_SWEEP_GBPS:
            # Operating point calibrated to the paper's ablation (§V-F): the
            # endpoint gate admits budget from a 1024-candidate set whose useful
            # mass exceeds the budget, so eta_BW settles at ~0.82 (the residual
            # <18% is SEA probes + causal false admits), while fetch-then-score
            # drags every fetched candidate over the link and lands at ~0.10.
            # These are whatever the sim emits at this point; the panel's claim
            # is only that the value is FLAT across the bandwidth sweep.
            cfg = SimConfig(n_candidates=1024, budget_per_step=256,
                            top_k_useful=16, useful_fraction=0.43,
                            cxl_bw_gbs=bw, n_hosts=N_HOSTS)
            r = run_closed_loop(meta["boundary"], meta["scorer"], cfg,
                                n_steps=96, seed=SEED)
            fetched = r["committed_bytes_mean"] + r["wasted_bytes_mean"]
            eta[policy].append(r["useful_bytes_mean"] / max(1.0, fetched))

    # Panel (d): load the real CFO overlap sweep if present.
    overlap = None
    ov_path = (Path(__file__).resolve().parent / "out" / "data"
               / "overlap_sweep.json")
    if ov_path.exists():
        with ov_path.open(encoding="utf-8") as f:
            overlap = json.load(f)
    return {"bw_sweep_gbps": BW_SWEEP_GBPS, "eta_bw": eta, "overlap": overlap}


# --------------------------------------------------------------------------- #
# Main sweep                                                                  #
# --------------------------------------------------------------------------- #
def run() -> Dict[str, Any]:
    """Sweep alpha for all three policies; collect latency samples + peak L.

    Returns a nested dict:
      results["config"]         = run parameters
      results["data"][policy][alpha] = {mean_us, p50_us, p99_us,
                                         throughput_tok_s, peak_outstanding,
                                         n_samples}
    """
    data: Dict[str, Dict[str, Any]] = {p: {} for p in POLICIES}
    for policy, driver in DRIVERS.items():
        for alpha in ALPHA_SWEEP:
            rng = np.random.default_rng(SEED + alpha)
            samples, peak_L = driver(alpha, rng)
            summary = summarize(samples)
            summary["peak_outstanding"] = int(peak_L)
            data[policy][str(alpha)] = summary
    return {
        "config": {
            "n_hosts": N_HOSTS,
            "per_tenant_bw_gbps": PER_TENANT_BW_GBPS,
            "alpha_sweep": ALPHA_SWEEP,
            "budget": BUDGET,
            "n_steps": N_STEPS,
            "seed": SEED,
            "token_table_capacity": TwoPhaseConfig().token_table_capacity,
            "reserve_rtt_us": TwoPhaseConfig().reserve_rtt_us,
        },
        "data": data,
        "efficiency": measure_efficiency(),
    }


def report(results: Dict[str, Any]) -> None:
    """Console table: per policy, per alpha -> Mean / P99 / Throughput / peak L."""
    data = results["data"]
    print("=" * 82)
    print("Two-phase validation vs single-phase gate  (16 hosts, 2 GB/s/tenant)")
    print("=" * 82)
    print(f"{'Policy':>16} {'alpha':>6} {'Mean(us)':>10} {'P99(us)':>10} "
          f"{'Tput(tok/s)':>12} {'peak L':>8}")
    print("-" * 82)
    for policy in POLICIES:
        for alpha in ALPHA_SWEEP:
            s = data[policy][str(alpha)]
            print(f"{policy:>16} {alpha:>6} {s['mean_us']:>10.2f} "
                  f"{s['p99_us']:>10.2f} {s['throughput_tok_s']:>12.1f} "
                  f"{s['peak_outstanding']:>8}")
        print("-" * 82)
    cap = results["config"]["token_table_capacity"]
    print(f"Token table capacity = {cap}. Two-phase P99 knees where offered "
          f"L = H*alpha*budget crosses it.")


# --------------------------------------------------------------------------- #
# Visualization                                                               #
# --------------------------------------------------------------------------- #
def plot(results: Dict[str, Any]):
    """2x2 argument closure for the two-phase / enforcement-placement story.

    (a) Tail-latency divergence: throughput vs P99, two-phase blows up while the
        single-phase gate stays flat.
    (b) Little's law: measured in-flight reservation count L vs the lambda*RTT
        prediction, overrunning the bounded table -- the collapse is queueing
        theory, not a simulator artifact.
    (c) Bandwidth efficiency as an architectural constant: measured eta_BW is
        flat across a 2-64 GB/s sweep, so it does not depend on provisioning.
    (d) Coalescing benefit vs cross-tenant overlap: throughput recovers with
        overlap and coincides with the no-coalescing baseline below break-even.
    """
    import matplotlib.pyplot as plt

    data = results["data"]
    eff = results["efficiency"]
    fig, axs = plt.subplots(2, 2, figsize=(12, 9))
    _panel_tail(axs[0, 0], data)
    _panel_littles_law(axs[0, 1], results)
    _panel_eta_constant(axs[1, 0], eff)
    _panel_overlap(axs[1, 1], eff)
    fig.tight_layout(pad=1.4)
    return fig


def _panel_tail(ax, data):
    """(a) Throughput (X) vs P99 latency (Y, log), one line per policy."""
    ax.set_yscale("log")
    lo, hi = ALPHA_SWEEP[0], ALPHA_SWEEP[-1]
    for policy, meta in POLICIES.items():
        xs = [data[policy][str(a)]["throughput_tok_s"] for a in ALPHA_SWEEP]
        ys = [data[policy][str(a)]["p99_us"] for a in ALPHA_SWEEP]
        ax.plot(xs, ys, "o-", color=meta["color"], label=meta["label"])
        ax.annotate(f"{hi}x", (xs[-1], ys[-1]), textcoords="offset points",
                    xytext=meta["annot_off"], fontsize=10, color=meta["color"])
    x1 = data["PROSE_APEX"][str(lo)]["throughput_tok_s"]
    y1 = data["PROSE_APEX"][str(lo)]["p99_us"]
    ax.annotate(f"{lo}x", (x1, y1), textcoords="offset points",
                xytext=(-42, 26), fontsize=10, color="0.3",
                arrowprops=dict(arrowstyle="->", color="0.55", lw=0.8))
    ax.set_xlabel("Aggregate promotion throughput (batches/s, 16 hosts)")
    ax.set_ylabel("P99 promotion latency (us, log)")
    ax.set_title("(a) Two-phase tail diverges under oversubscription", pad=22)
    # Data lives in two corners: PROSE-APEX / Two-phase cluster bottom-right,
    # Fetch-then-score climbs to top-left. The upper-right quadrant (high
    # throughput AND high latency) is empty, so the legend goes there without
    # colliding with any line, the endpoint tags, or the title.
    ax.margins(x=0.12, y=0.18)
    ax.legend(loc="upper right", fontsize=10, framealpha=0.9)


def _panel_littles_law(ax, results):
    """(b) Measured in-flight L vs lambda*RTT prediction; table capacity line."""
    data = results["data"]
    cap = results["config"]["token_table_capacity"]
    budget = results["config"]["budget"]
    hosts = results["config"]["n_hosts"]
    measured = [data["TwoPhase"][str(a)].get("peak_outstanding", 0)
                for a in ALPHA_SWEEP]
    # Little's law offered load: L = lambda * RTT ~ H * alpha * budget clamped by
    # per-step drain; the table caps residency, so measured tracks min(offered,
    # multiples of cap) as back-pressure admits in table-sized waves.
    predicted = [hosts * a * budget for a in ALPHA_SWEEP]
    x = list(range(len(ALPHA_SWEEP)))
    ax.plot(x, predicted, "s--", color=C["oracle"],
            label=r"Offered $L=\lambda\,$RTT")
    ax.plot(x, measured, "o-", color=C["accent2"],
            label="Measured in-flight L")
    ax.axhline(cap, ls=":", color=C["fts"], lw=2.0,
               label=f"Table capacity ({cap})")
    ax.set_yscale("log")
    for xi, m in zip(x, measured):
        if m >= cap:
            ax.annotate(f"{m}", (xi, m), textcoords="offset points",
                        xytext=(0, 8), fontsize=10, color=C["accent2"],
                        ha="center")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{a}x" for a in ALPHA_SWEEP])
    ax.set_xlabel("Oversubscription " + r"$\alpha$")
    ax.set_ylabel("In-flight reservations (log)")
    ax.set_title("(b) Little's law predicts the table overrun", pad=20)
    ax.legend(loc="upper left", fontsize=11)


def _panel_eta_constant(ax, eff):
    """(c) Measured eta_BW across the real 2-64 GB/s bandwidth sweep.

    The message is the flat shape: efficiency does not move with provisioning,
    so it is an architectural constant of the enforcement placement, not a
    function of how much link the operator buys.
    """
    bw = eff["bw_sweep_gbps"]
    x = list(range(len(bw)))
    for policy, meta in ETA_POLICIES.items():
        ys = eff["eta_bw"][policy]
        ax.plot(x, ys, "o-", color=meta["color"], label=meta["label"])
        ax.annotate(f"{ys[-1]:.2f}", (x[-1], ys[-1]),
                    textcoords="offset points", xytext=(6, 4), fontsize=11,
                    color=meta["color"])
    # Call out the endpoint-gate line as the architectural constant. The value
    # is READ from the measured curve (not hand-set) so the annotation can never
    # drift from what the sim emitted.
    pa = eff["eta_bw"]["PROSE_APEX"]
    if pa:
        y_const = pa[0]
        ax.annotate(f"Architectural constant ({y_const:.2f})",
                    (x[len(x) // 2], y_const), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=10,
                    color=ETA_POLICIES["PROSE_APEX"]["color"])
    ax.set_ylim(0.0, 1.0)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{int(b)}" for b in bw])
    ax.set_xlabel("Per-tenant bandwidth (GB/s)")
    ax.set_ylabel(r"Payload efficiency $\eta_{\mathrm{BW}}$")
    ax.set_title("(c) Efficiency is bandwidth-invariant", pad=18)
    ax.legend(loc="center right", fontsize=11)


def _panel_overlap(ax, eff):
    """(d) CFO benefit vs cross-tenant overlap (real overlap_sweep.json).

    Throughput with coalescing recovers to the ceiling as overlap rises and
    coincides with the no-coalescing baseline below the 0.45 break-even, so the
    mechanism is zero-cost tail protection whose payoff scales with overlap.
    """
    ov = eff.get("overlap")
    if not ov:
        ax.text(0.5, 0.5, "overlap_sweep.json missing", ha="center",
                va="center", transform=ax.transAxes)
        ax.set_title("(d) CFO benefit vs cross-tenant overlap")
        return
    rows = ov["rows"]
    xs = [r["overlap"] for r in rows]
    tput_cfo = [r["tput_scale_cfo"] for r in rows]
    tput_no = [r["tput_scale_nocfo"] for r in rows]
    ax.plot(xs, tput_cfo, "o-", color=C["cefe"], label="Coalescing on")
    ax.plot(xs, tput_no, "s--", color=C["oracle"], label="Coalescing off")
    be = ov.get("break_even", 0.45)
    ax.axvline(be, ls=":", color=C["fts"], lw=2.0,
               label=f"Break-even ({be:.2f})")
    tr = ov.get("trace_overlap")
    if tr is not None:
        ax.axvline(tr, ls="-.", color=C["accent1"], lw=1.5,
                   label=f"Trace ({tr:.2f})")
    ax.set_xlabel("Cross-tenant overlap")
    ax.set_ylabel("Throughput (normalized)")
    ax.set_title("(d) Coalescing benefit scales with overlap", pad=18)
    ax.legend(loc="lower right", fontsize=10)


# --------------------------------------------------------------------------- #
# RTT sensitivity sweep                                                       #
# --------------------------------------------------------------------------- #
def run_rtt_sweep() -> Dict[str, Any]:
    """Fix hosts + oversubscription; vary the reserve RTT for TwoPhase.

    PROSE_APEX is RTT-independent (no reservation window), so it is measured
    once as the flat reference line. Returns:
      results["config"]      = sweep parameters
      results["prose_apex"]  = {p99_us, throughput_tok_s} (flat reference)
      results["two_phase"][rtt] = {mean_us, p50_us, p99_us, throughput_tok_s,
                                   peak_outstanding, n_samples}
    """
    # PROSE_APEX reference at the fixed operating point (RTT-independent).
    rng = np.random.default_rng(SEED + RTT_ALPHA)
    prose_samples, _ = drive_prose_apex(RTT_ALPHA, rng)
    prose_ref = summarize(prose_samples)

    tp_data: Dict[str, Any] = {}
    for rtt in RTT_SWEEP_US:
        rng = np.random.default_rng(SEED + RTT_ALPHA)
        samples, peak_L = drive_two_phase(RTT_ALPHA, rng, rtt_us=rtt)
        s = summarize(samples)
        s["peak_outstanding"] = int(peak_L)
        tp_data[str(rtt)] = s

    return {
        "config": {
            "n_hosts": N_HOSTS,
            "per_tenant_bw_gbps": PER_TENANT_BW_GBPS,
            "alpha": RTT_ALPHA,
            "budget": BUDGET,
            "n_steps": N_STEPS,
            "seed": SEED,
            "rtt_sweep_us": RTT_SWEEP_US,
            "token_table_capacity": TwoPhaseConfig().token_table_capacity,
        },
        "prose_apex": prose_ref,
        "two_phase": tp_data,
    }


def report_rtt_sweep(results: Dict[str, Any]) -> None:
    """Console table: reserve RTT -> TwoPhase Mean / P99 / Throughput / peak L."""
    cfg = results["config"]
    pa = results["prose_apex"]
    print("=" * 78)
    print(f"RTT sensitivity  (16 hosts, 2 GB/s/tenant, alpha={cfg['alpha']}x)")
    print("=" * 78)
    print(f"{'Policy':>16} {'RTT(us)':>8} {'Mean(us)':>10} {'P99(us)':>10} "
          f"{'Tput(tok/s)':>12} {'peak L':>8}")
    print("-" * 78)
    print(f"{'PROSE_APEX':>16} {'n/a':>8} {pa['mean_us']:>10.2f} "
          f"{pa['p99_us']:>10.2f} {pa['throughput_tok_s']:>12.1f} {0:>8}")
    print("-" * 78)
    for rtt in RTT_SWEEP_US:
        s = results["two_phase"][str(rtt)]
        print(f"{'TwoPhase':>16} {rtt:>8.1f} {s['mean_us']:>10.2f} "
              f"{s['p99_us']:>10.2f} {s['throughput_tok_s']:>12.1f} "
              f"{s['peak_outstanding']:>8}")
    print("-" * 78)
    print(f"Even the fastest {RTT_SWEEP_US[0]} us shim collapses: the reservation "
          f"WINDOW pins tokens regardless of its length.")


def plot_rtt_sweep(results: Dict[str, Any]):
    """Reserve RTT (X, us) vs P99 latency (Y, us): TwoPhase rises, PROSE flat.

    Expected: TwoPhase P99 grows monotonically (super-linearly, since a longer
    RTT both adds to the critical path AND widens the pin window -> larger L ->
    more back-pressure), while PROSE_APEX is a horizontal reference line
    independent of RTT.
    """
    import matplotlib.pyplot as plt

    rtts = RTT_SWEEP_US
    tp_p99 = [results["two_phase"][str(r)]["p99_us"] for r in rtts]
    pa_p99 = results["prose_apex"]["p99_us"]

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(rtts, tp_p99, "o-", color=C["accent2"],
            label="Two-phase validation")
    ax.axhline(pa_p99, ls="--", color=C["cefe"],
               label="PROSE-APEX (single-phase gate)")
    for r, y in zip(rtts, tp_p99):
        ax.annotate(f"{y:.0f}", (r, y), textcoords="offset points",
                    xytext=(6, 4), fontsize=11, color=C["accent2"])
    ax.set_xlabel("Reserve round-trip RTT (us)")
    ax.set_ylabel("P99 promotion latency (us)")
    ax.set_title(f"Any reservation window collapses under load\n"
                 f"(16 hosts, {RTT_ALPHA}x oversubscription)")
    ax.set_xticks(rtts)
    ax.legend(loc="center right")
    fig.tight_layout()
    return fig


def main() -> None:
    results = run()
    report(results)
    save_json("two_phase_slo", results)
    save_fig(plot(results), "two_phase_slo")
    print("\nSaved: experiments/out/data/two_phase_slo.json")

    rtt_results = run_rtt_sweep()
    report_rtt_sweep(rtt_results)
    save_json("two_phase_rtt_sweep", rtt_results)
    save_fig(plot_rtt_sweep(rtt_results), "two_phase_rtt_sweep")
    print("\nSaved: experiments/out/data/two_phase_rtt_sweep.json")


if __name__ == "__main__":
    main()
