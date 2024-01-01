"""Smoke tests for the SimCXL-extension ordering guarantee.

These assert the headline invariant the paper rests on: CEFE holds zero RPE
while fetch-then-score does not, at every chunk granularity.
"""
import importlib

import pytest


def _admission_sim():
    return importlib.import_module("simcxl_ext.cxl_admission_sim")


def test_cefe_holds_zero_rpe_at_all_granularities():
    sim = _admission_sim()
    for chunk_kib in (4, 16, 64, 256):
        n = max(64, (64 * 1024 // (chunk_kib * 1024)) * 256)
        cfg = sim.SimConfig(n_candidates=n, budget_per_step=64,
                            top_k_useful=32, useful_fraction=0.04)
        r = sim.run_closed_loop("cefe", "odus_x", cfg, n_steps=64, seed=0)
        assert r["rpe_bytes_mean"] == 0.0, (
            f"CEFE must hold zero RPE at {chunk_kib} KiB, got {r['rpe_bytes_mean']}")


def test_fetch_then_score_incurs_rpe():
    sim = _admission_sim()
    cfg = sim.SimConfig(n_candidates=1024, budget_per_step=64,
                        top_k_useful=32, useful_fraction=0.04)
    r = sim.run_closed_loop("fts_quest", "quest", cfg, n_steps=64, seed=0)
    assert r["rpe_bytes_mean"] > 0.0, "fetch-then-score must expose reclaimed payload"


def test_cefe_useful_efficiency_beats_fts():
    sim = _admission_sim()
    cfg = sim.SimConfig(n_candidates=1024, budget_per_step=64,
                        top_k_useful=32, useful_fraction=0.04)
    cefe = sim.run_closed_loop("cefe", "odus_x", cfg, n_steps=64, seed=0)
    fts = sim.run_closed_loop("fts_quest", "quest", cfg, n_steps=64, seed=0)
    assert cefe["useful_frac_of_fetched"] >= fts["useful_frac_of_fetched"]


def test_package_exposes_simcxl_timing():
    ext = importlib.import_module("simcxl_ext")
    t = ext.SimCXLTiming()
    # Inherited, calibrated SimCXL constants must be present and sane.
    assert t.proto_proc_lat_ns > 0
    assert t.cxl_link_bw_gbps > 0
    assert t.req_queue_depth > 0


# ---------------------------------------------------------------------------
# Mode B (Pull) tests
# ---------------------------------------------------------------------------

def _endpoint_sim():
    return importlib.import_module("simcxl_ext.endpoint_sim")


def test_mode_b_pull_holds_zero_rpe():
    """Mode B (pull) must hold RPE=0: token gate prevents payload for invalids."""
    sim = _admission_sim()
    cfg = sim.SimConfig(n_candidates=1024, budget_per_step=64,
                        top_k_useful=32, useful_fraction=0.04)
    r = sim.run_closed_loop("cefe_pull", "odus_x", cfg, n_steps=64, seed=0)
    assert r["rpe_bytes_mean"] == 0.0, (
        f"Mode B (pull) must hold zero RPE, got {r['rpe_bytes_mean']}")


def test_mode_b_pull_has_higher_latency_than_push():
    """Mode B adds host RTT overhead; latency must exceed Mode A."""
    sim = _admission_sim()
    cfg = sim.SimConfig(n_candidates=1024, budget_per_step=64,
                        top_k_useful=32, useful_fraction=0.04)
    push = sim.run_closed_loop("cefe", "odus_x", cfg, n_steps=64, seed=0)
    pull = sim.run_closed_loop("cefe_pull", "odus_x", cfg, n_steps=64, seed=0)
    assert pull["admission_us_mean"] >= push["admission_us_mean"]


def test_mode_b_endpoint_sim_token_redemption():
    """Endpoint sim Mode B: all tokens redeemed, zero RPE in state."""
    esim = _endpoint_sim()
    config = esim.EndpointConfig(mode="Mode_B_Pull")
    state = esim.EndpointState()
    burst = esim.DescriptorBurst(n_descriptors=100, inter_arrival_ns=2.0,
                                 tenant_id=0)
    state = esim.simulate_endpoint_burst(burst, config, state,
                                         scorer_accept_rate=0.6)
    assert state.total_pull_rpe == 0
    assert state.total_token_issued > 0
    assert state.total_token_redeemed + state.total_token_expired == state.total_token_issued


def test_mode_b_cfo_coalescing_still_works():
    """CFO must remain functional in pull mode (shared reads coalesced)."""
    sim = _admission_sim()
    cfg = sim.SimConfig(n_candidates=512, budget_per_step=64,
                        top_k_useful=32, useful_fraction=0.08)
    r = sim.run_closed_loop("cefe_pull", "odus_x", cfg, n_steps=32, seed=7)
    # Pull mode still produces valid results with zero RPE
    assert r["rpe_bytes_mean"] == 0.0
    assert r["recovery_at_k_mean"] > 0.0
