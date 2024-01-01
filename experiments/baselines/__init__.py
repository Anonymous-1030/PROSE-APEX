"""Mechanism-level baseline comparison package.

Importing this package registers all mechanism specs into
``baseline_common.SPECS`` in the canonical ``METHOD_ORDER``.
"""
from __future__ import annotations

from . import baseline_common  # noqa: F401
from . import nocheck          # noqa: F401
from . import shared_refcount  # noqa: F401
from . import two_phase_reservation  # noqa: F401
from . import generation_only  # noqa: F401
from . import genonly_epoch_fence  # noqa: F401
from . import rdma_key         # noqa: F401
from . import segmented_dma    # noqa: F401
from . import prose            # noqa: F401

from .baseline_common import SPECS, METHOD_ORDER


def ordered_specs():
    """Return the registered mechanism specs in canonical METHOD_ORDER."""
    missing = [m for m in METHOD_ORDER if m not in SPECS]
    if missing:
        raise RuntimeError(f"unregistered specs: {missing}")
    return [SPECS[m] for m in METHOD_ORDER]
