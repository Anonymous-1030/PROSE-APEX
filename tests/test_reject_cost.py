"""Tests for the reject/retry cost study (`experiments/run_reject_cost.py`).

Asserts the quantitative properties the commit-time-gate cost story rests on:
  R1. The throughput-loss decomposition is an exact accounting identity:
      offered payload bytes = valid + stale (RPE) + rejected payload,
      for every mechanism at 8/32/64x oversubscription.
  R2. A PROSE reject is metadata-only: the reject-metadata overhead is below
      0.2% of the offered payload bytes at EVERY oversubscription point.
  R3. The REFCNT_S retry-count distribution reconstructed per descriptor from
      the shared trace has the same mean as the design-space study's
      `retries_per_descriptor` in results/design_space.json (and no other
      mechanism retries at all).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.run_design_space import OVERSUB, SEEDS, GRID_MECHS
from experiments.run_reject_cost import ALL_MECHS, compute_cell

FAST_SEEDS = SEEDS[:2]
DESIGN_SPACE_JSON = Path(__file__).resolve().parent.parent / "results" / "design_space.json"


# R1 ─ decomposition accounting identity ──────────────────────────────────────
def test_decomposition_identity_holds():
    for mech in ALL_MECHS:
        for oversub in (8, 32, 64):
            c = compute_cell(mech, oversub, FAST_SEEDS)
            d = c["decomposition"]
            # byte-level identity
            assert (c["valid_payload_bytes"] + c["stale_payload_bytes"]
                    + c["rejected_payload_bytes"]
                    == pytest.approx(c["offered_payload_bytes"], abs=1e-3)), \
                (mech, oversub)
            # fraction-level identity
            assert (d["valid_frac"] + d["stale_rpe_frac"]
                    + d["rejected_payload_frac"]) == pytest.approx(1.0, abs=1e-12), \
                (mech, oversub)
            # rejected payload is exactly the rejected descriptors' worth
            assert c["rejected_descriptors"] == \
                c["offered_descriptors"] - c["admitted_descriptors"]


# R2 ─ PROSE reject byte cost < 0.2% of offered payload at all oversub ────────
def test_prose_reject_byte_cost_below_0p2_percent_all_oversub():
    for oversub in OVERSUB:
        c = compute_cell("PROSE", oversub, FAST_SEEDS)
        frac = c["reject_metadata_bytes"] / c["offered_payload_bytes"]
        assert frac < 0.002, (oversub, frac)
        # and it is metadata ONLY: rejected descriptors carry zero payload
        assert c["decomposition"]["reject_metadata_frac"] == frac
        assert c["stale_payload_bytes"] == 0.0


# R3 ─ REFCNT_S retry distribution mean matches the design-space study ────────
def test_refcnt_s_retry_mean_matches_design_space():
    if not DESIGN_SPACE_JSON.exists():
        pytest.skip("results/design_space.json not present "
                    "(run experiments/run_design_space.py first)")
    ds = json.loads(DESIGN_SPACE_JSON.read_text())["table_iv_grid_capacity"]["REFCNT_S"]
    for oversub in OVERSUB:
        c = compute_cell("REFCNT_S", oversub, SEEDS)
        dist = c["retry_dist"]
        n_ad = c["admitted_descriptors"]
        assert dist["0"] + dist["1"] + dist["2+"] == n_ad
        dist_mean = (dist["1"] + 2 * dist["2+"]) / n_ad
        assert dist_mean == c["retries_per_descriptor"]
        assert dist_mean == pytest.approx(
            ds[str(oversub)]["retries_per_descriptor"], abs=1e-12), oversub
        # engine model: at most one re-select retry per descriptor
        assert dist["2+"] == 0
        assert dist["1"] > 0                       # the tax is real, not zero


def test_only_refcnt_s_retries():
    for mech in GRID_MECHS + ["PROSE_HOSTSEL"]:
        if mech == "REFCNT_S":
            continue
        c = compute_cell(mech, 32, FAST_SEEDS)
        assert c["retries"] == 0
        assert c["retry_added_ns_per_descriptor"] == 0.0
        assert c["retry_dist"]["1"] == 0 and c["retry_dist"]["2+"] == 0
