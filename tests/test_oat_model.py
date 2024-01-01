"""Pytest wrapper for the exhaustive OAT model checker (formal/check_oat_model.py).

This is the Java-free mirror of formal/prose_oat.tla. It verifies the safety
contract (Invariant 1 / Theorem 1) by exhaustively exploring the finite instance
declared in formal/prose_oat.cfg, and it confirms the check is non-vacuous by
showing that removing the pin guard on reclaim produces a stale-payload
counterexample.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "formal"))

import check_oat_model as oat


def test_oat_safety_holds():
    """Safe model: all invariants hold across the whole reachable state space."""
    n, violation = oat.check(allow_unsafe_reclaim=False)
    assert violation is None, f"invariant violated: {violation}"
    assert n > 0


def test_pinless_reclaim_is_unsafe():
    """Falsifiability: dropping the PIN=0 reclaim guard MUST expose stale payload."""
    n, violation = oat.check(allow_unsafe_reclaim=True)
    assert violation is not None, (
        "pin-less reclaim found no violation; the pin discipline is not what "
        "enforces zero-RPE — the model would be vacuous")
    inv, _state = violation
    assert inv == "InvZeroRPE"
