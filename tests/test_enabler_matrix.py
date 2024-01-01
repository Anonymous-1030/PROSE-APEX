"""Acceptance tests for the enabler matrix (2x2 commit-gate x pool-autonomy).

These assert the qualitative properties the ENABLER narrative rests on, not
tuned magic numbers (small seed count; the full 10-seed grid is produced by
experiments/run_enabler_matrix.py):

  E1. gate OFF + autonomy ON exposes RPE_payload > 0 (GENONLY-class, ~17% at
      32x) — budget enforcement without protection is undeployable.
  E2. EXACTLY ONE cell — gate ON + autonomy ON (the PROSE operating point) —
      achieves RPE == 0 AND sustained valid throughput == 1.0x.
  E3. Both autonomy-OFF cells exhaust the pool and lose >= 50% of sustained
      valid throughput after exhaustion (reclaimable capacity ~ 0).
"""
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.oversub_reclaim import (
    OversubConfig, generate_oversub_trace, replay_enabler_cell,
)

SEEDS = [0, 1, 2]
CELLS = {  # key -> (gate_on, autonomy_on)
    "gateON_autoON": (True, True),
    "gateON_autoOFF": (True, False),
    "gateOFF_autoON": (False, True),
    "gateOFF_autoOFF": (False, False),
}


def _run_matrix():
    out = {}
    for key, (gate, aut) in CELLS.items():
        runs = []
        for seed in SEEDS:
            cfg = OversubConfig(oversubscription=32, n_tenants=16,
                                admit_budget=32, n_steps=200, capacity=512,
                                token_table=512, bound_mode="capacity",
                                seed=seed)
            runs.append(replay_enabler_cell(generate_oversub_trace(cfg),
                                            gate, aut))
        out[key] = runs
    return out


_MATRIX = _run_matrix()


def _sustained_norm(key):
    """Per-seed sustained valid throughput, normalized (paired) to the
    gate-ON + autonomy-ON cell."""
    base = _MATRIX["gateON_autoON"]
    return [r["sustained_valid_Bpstep"] / b["sustained_valid_Bpstep"]
            for r, b in zip(_MATRIX[key], base)]


def test_gate_off_autonomy_on_rpe_positive():
    rpe = mean(r["rpe_payload_frac"] for r in _MATRIX["gateOFF_autoON"])
    assert rpe > 0.0
    assert rpe > 0.10                      # GENONLY-class (~0.17), not a trace
    for r in _MATRIX["gateOFF_autoON"]:
        assert r["rpe_events"] > 0


def test_exactly_one_deployable_cell():
    deployable = []
    for key in CELLS:
        rpe = mean(r["rpe_payload_frac"] for r in _MATRIX[key])
        thr = mean(_sustained_norm(key))
        if rpe == 0.0 and thr >= 0.99:
            deployable.append(key)
    assert deployable == ["gateON_autoON"], deployable


def test_autonomy_off_cells_collapse_after_exhaustion():
    for key in ("gateON_autoOFF", "gateOFF_autoOFF"):
        runs = _MATRIX[key]
        # the pool exhausts in every seed ...
        assert all(r["exhaustion_step"] >= 0 for r in runs)
        assert all(r["n_failed_admissions"] > 0 for r in runs)
        # ... reclaimable capacity collapses to ~0 ...
        assert mean(r["reclaimable_frac_mean"] for r in runs) < 0.30
        # ... and sustained valid throughput falls by at least 50%
        thr = mean(_sustained_norm(key))
        assert thr <= 0.5, (key, thr)
