"""Tests for Experiment A (optimistic reclaim) and Experiment B (page cache).

These assert the *qualitative* properties the innovation narrative rests on, not
tuned magic numbers:
  A1. PROSE reclaimable capacity is ~independent of oversubscription.
  A2. Early protection (REFCNT/2PHASE) reclaimable capacity collapses at high
      oversubscription while PROSE's stays high.
  A3. PROSE P99 admission latency stays flat; 2PHASE's grows with oversub.
  A4. GENONLY exposes RPE > 0; PROSE/REFCNT/2PHASE keep RPE == 0.
  A5. Both back-pressure models (capacity, token_table) agree qualitatively.
  B1. A page cache shows unmitigated RPE > 0 under the four exposure conditions.
  B2. The OAT gate drives page-cache RPE to 0.
  B3. Early protection's page-cache protection span >> PROSE's.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.oversub_reclaim import (
    OversubConfig, generate_oversub_trace, replay_oversub, MECHS,
)
from experiments.page_cache_instance import (
    PageConfig, measure_page_rpe, protection_duration_ratio,
)


def _run(oversub, mech, bound_mode="capacity", tenants=16, cap=512):
    cfg = OversubConfig(oversubscription=oversub, n_tenants=tenants,
                        admit_budget=32, n_steps=200, capacity=cap,
                        token_table=cap, bound_mode=bound_mode, seed=0)
    return replay_oversub(generate_oversub_trace(cfg), MECHS[mech])


def test_prose_reclaimable_stable_across_oversub():
    lo = _run(8, "PROSE")["reclaimable_capacity_frac"]
    hi = _run(64, "PROSE")["reclaimable_capacity_frac"]
    assert abs(hi - lo) < 0.05, (lo, hi)
    assert hi > 0.8


def test_early_protection_reclaimable_collapses():
    prose = _run(32, "PROSE")["reclaimable_capacity_frac"]
    refcnt = _run(32, "REFCNT")["reclaimable_capacity_frac"]
    assert prose > 0.8
    assert refcnt < 0.1
    assert prose > refcnt + 0.7


def test_prose_p99_flat_twophase_grows():
    prose_lo = _run(8, "PROSE")["admission_p99_ns"]
    prose_hi = _run(64, "PROSE")["admission_p99_ns"]
    tp_lo = _run(8, "2PHASE")["admission_p99_ns"]
    tp_hi = _run(64, "2PHASE")["admission_p99_ns"]
    assert prose_hi <= prose_lo * 1.5 + 1        # PROSE flat
    assert tp_hi >= tp_lo                          # 2Phase non-decreasing
    assert tp_hi > prose_hi * 100                  # orders of magnitude worse


def test_rpe_only_for_genonly():
    assert _run(32, "GENONLY")["rpe_payload_frac"] > 0.05
    for m in ("PROSE", "REFCNT", "2PHASE"):
        assert _run(32, m)["rpe_payload_frac"] == 0.0


def test_bound_modes_agree_qualitatively():
    cap = _run(32, "REFCNT", bound_mode="capacity")["reclaimable_capacity_frac"]
    tok = _run(32, "REFCNT", bound_mode="token_table")["reclaimable_capacity_frac"]
    assert cap < 0.1 and tok < 0.1                 # both collapse


def test_page_cache_unmitigated_rpe_positive():
    u = measure_page_rpe(PageConfig(policy="LRU"), gated=False)
    assert u["rpe_payload_frac"] > 0.05            # exposure conditions hold


def test_page_cache_oat_gate_zeros_rpe():
    g = measure_page_rpe(PageConfig(policy="LRU"), gated=True)
    assert g["rpe_payload_frac"] == 0.0
    assert g["rejected_reads"] > 0                 # stale prefetches rejected


def test_page_cache_protection_span():
    pd = protection_duration_ratio(PageConfig())
    assert abs(pd["prose_span_ratio"] - 1.0) < 1e-9
    assert pd["refcnt_span_ratio"] > 10            # queue-wide protection is huge


# ── Design-space study (rebuttal: "REFCNT strawman" + reclaim-side checking) ──
# Additive assertions for the three new ProtMech entries (REFCNT_S,
# RECLAIM_DEFER, PROSE_HOSTSEL). Small seed counts keep the tests fast; the
# asserted effects are large (cliff vs no-cliff, ~17% stale admits), so two
# seeds are ample.

def _run_seeds(oversub, mech, seeds=(0, 1), bound_mode="capacity", tenants=16,
               cap=512):
    """Replay `mech` at `oversub` for a few seeds; returns the per-seed dicts."""
    out = []
    for seed in seeds:
        cfg = OversubConfig(oversubscription=oversub, n_tenants=tenants,
                            admit_budget=32, n_steps=200, capacity=cap,
                            token_table=cap, bound_mode=bound_mode, seed=seed)
        out.append(replay_oversub(generate_oversub_trace(cfg), MECHS[mech]))
    return out


def test_refcnt_subset_reclaimable_exceeds_refcnt_64x():
    # Pinning only the host's budget-sized subset keeps pool occupancy ~= budget,
    # so REFCNT_S's reclaimable capacity stays high where REFCNT's collapses.
    refcnt = _run(64, "REFCNT")["reclaimable_capacity_frac"]
    refcnt_s = _run(64, "REFCNT_S")["reclaimable_capacity_frac"]
    assert refcnt_s > refcnt                       # strictly greater
    assert refcnt_s > 0.8


def test_reclaim_defer_cliff_coincides_with_refcnt():
    # Reclaim-side deferral is pre-enqueue protection implemented on the reclaim
    # path: the SAME occupancy, hence the SAME cliff position as REFCNT.
    for oversub in (8, 16, 32, 64):
        rd = _run(oversub, "RECLAIM_DEFER")["reclaimable_capacity_frac"]
        rc = _run(oversub, "REFCNT")["reclaimable_capacity_frac"]
        assert abs(rd - rc) < 1e-9, (oversub, rd, rc)
    # throughput collapses with it: <= 0.6x of PROSE at 64x
    prose = _run(64, "PROSE")["valid_throughput_Bpns"]
    rd_thr = _run(64, "RECLAIM_DEFER")["valid_throughput_Bpns"]
    assert rd_thr <= 0.6 * prose
    # deferral ends at dequeue and there is no transfer-span pin: stale payload
    assert _run(32, "RECLAIM_DEFER")["rpe_payload_frac"] > 0.05


def test_commit_time_mechanisms_zero_stale_regardless_of_authority():
    # Once the generation check is at commit time, WHO selects the transfer set
    # (endpoint vs host pre-selection) does not affect safety.
    for m in ("PROSE", "PROSE_HOSTSEL"):
        for r in _run_seeds(32, m):
            assert r["stale_admits"] == 0
            assert r["stale_bytes"] == 0.0
            assert r["rpe_payload_frac"] == 0.0
    p = _run(32, "PROSE")
    h = _run(32, "PROSE_HOSTSEL")
    assert abs(p["reclaimable_capacity_frac"]
               - h["reclaimable_capacity_frac"]) < 1e-9
    assert abs(p["valid_throughput_Bpns"]
               - h["valid_throughput_Bpns"]) < 1e-12


def test_refcnt_subset_stale_admits_positive_at_8x_and_above():
    # The cliff is gone only because identity-only pins skip the generation
    # check: reclaim+reincarnation in the snapshot->acquire window yields stale
    # admits (and re-select retries when the object is simply gone).
    for oversub in (8, 32, 64):
        for r in _run_seeds(oversub, "REFCNT_S"):
            assert r["stale_admit_rate"] > 0.0, oversub
            assert r["stale_admits"] > 0, oversub
            assert r["retries"] > 0, oversub
    rates = [r["stale_admit_rate"] for r in _run_seeds(32, "REFCNT_S")]
    assert sum(rates) / len(rates) > 0.05          # not a trace-level effect
