"""Tests for the mechanistic RPE binding model (trace_adapter/rpe_binding_model.py).

These check the *properties* the model must have (not tuned magic numbers):
  1. With no queue delay, RPE is zero (a descriptor issues before any reuse).
  2. RPE increases monotonically as mean queue residence grows.
  3. A pool large enough to never evict yields zero RPE regardless of residence.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "trace_adapter"))

import rpe_binding_model as rbm

EXP = Path(__file__).resolve().parent.parent / "experiments"
TRACE = EXP / "azure_conv_8t.csv"


def _available():
    return TRACE.exists()


def test_zero_residence_zero_rpe():
    if not _available():
        return
    r = rbm.measure_rpe(str(TRACE), 256, "LRU", 8, queue_delay=1, max_events=3000)
    assert r.rpe_payload < 0.01, f"near-zero residence should give ~0 RPE, got {r.rpe_payload}"


def test_rpe_monotone_in_residence():
    if not _available():
        return
    vals = [rbm.measure_rpe(str(TRACE), 256, "LRU", 16, queue_delay=q,
                            max_events=3000).rpe_payload
            for q in (8, 32, 128)]
    assert vals[0] <= vals[1] + 1e-9 <= vals[2] + 1e-9, f"RPE not monotone: {vals}"
    assert vals[2] > vals[0], "RPE must grow with queue residence"


def test_no_eviction_no_rpe():
    if not _available():
        return
    # Huge pool: nothing is ever evicted, so no binding can change -> zero RPE.
    r = rbm.measure_rpe(str(TRACE), 100000, "LRU", 16, queue_delay=256, max_events=3000)
    assert r.evictions == 0
    assert r.rpe_payload == 0.0
