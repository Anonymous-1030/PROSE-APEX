"""Tests for the RPE stale-byte harm surrogate and the cross-tenant leak PoC.

Both artifacts are mechanistic simulations (no real LLM); these tests check the
properties the surrogates must have:
  1. NIAH retrieval success at s=0 is exactly 1.0 (no stale bytes, no harm).
  2. Mean curves are non-increasing for NIAH/RULER and non-decreasing for KL.
  3. The PoC leaks > 0 cross-tenant bytes with the gate off and exactly 0 with
     the commit-time gate on (which completes metadata-only).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments import run_cross_tenant_leak as poc
from experiments import run_rpe_harm as harm


@pytest.fixture(scope="module")
def harm_results():
    # Reduced trial count keeps the test fast; seeds/streams are unchanged.
    return harm.run(n_trials=600)


@pytest.fixture(scope="module")
def poc_results():
    return poc.run(n_windows=200)


def test_niah_success_at_zero_stale(harm_results):
    pt0 = harm_results["metrics"]["niah"]["points"][0]
    assert pt0["s_pct"] == 0
    assert pt0["mean"] == 1.0
    assert pt0["counters"]["failures"] == pt0["counters"]["stale_needle_events"] == 0


def test_mean_curves_monotone(harm_results):
    m = harm_results["metrics"]
    niah = [p["mean"] for p in m["niah"]["points"]]
    ruler = [p["mean"] for p in m["ruler"]["points"]]
    kl = [p["mean"] for p in m["kl"]["points"]]
    assert all(a >= b - 1e-12 for a, b in zip(niah, niah[1:])), \
        f"NIAH mean not non-increasing: {niah}"
    assert all(a >= b - 1e-12 for a, b in zip(ruler, ruler[1:])), \
        f"RULER mean not non-increasing: {ruler}"
    assert all(a <= b + 1e-12 for a, b in zip(kl, kl[1:])), \
        f"KL mean not non-decreasing: {kl}"
    # The scripts' own monotonicity records must agree.
    assert m["niah"]["monotonicity"]["ok"]
    assert m["ruler"]["monotonicity"]["ok"]
    assert m["kl"]["monotonicity"]["ok"]


def test_mechanism_counters_exact(harm_results):
    for p in harm_results["metrics"]["niah"]["points"]:
        c = p["counters"]
        assert c["failures"] == c["stale_needle_events"]
        assert c["successes"] + c["failures"] == c["trials"]
    for p in harm_results["metrics"]["ruler"]["points"]:
        c = p["counters"]
        assert c["incorrect"] == c["chains_with_any_stale_chunk"]
        assert c["correct"] + c["incorrect"] == c["trials"]


def test_poc_gate_off_leaks_gate_on_zero(poc_results):
    lk = poc_results["leak"]
    assert lk["gate_off_cross_tenant_bytes"] > 0
    assert lk["gate_off_windows_with_leak"] == poc_results["config"]["n_race_windows"]
    assert lk["gate_on_cross_tenant_bytes"] == 0
    assert lk["gate_on_payload_bytes_moved"] == 0
    # Byte-level identity: A received tenant B's pattern, not its own.
    hx = poc_results["hex_excerpt_window0"]
    assert hx["a_received_gate_off_hex"] != hx["a_expected_hex"]
