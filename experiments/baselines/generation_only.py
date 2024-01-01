"""Generation-check-without-pin baseline.

The descriptor carries ``object_id`` and ``expected_epoch``.  At endpoint
admission the current mapping is read; if the object is resident and the epoch
matches, the transfer is allowed, otherwise it is rejected.  A successful check
does NOT increment a pin count and does NOT block eviction or slot reuse — the
object may be evicted DURING the transfer.

This baseline demonstrates that endpoint generation validation alone is
insufficient: validation must be bound to the transfer lifetime.  Under the
race-stress workload the sequence

    generation check succeeds -> object evicted / slot reused -> payload
    transfer not yet complete

produces stale payload for the post-eviction bytes of the (non-preemptible)
transfer.  It still rejects descriptors that went stale WHILE QUEUED, so a
queue-time race yields a reject (no payload), not stale bytes.
"""
from __future__ import annotations

from .baseline_common import MechanismSpec, register_spec

SPEC = register_spec(MechanismSpec(
    name="GenOnly",
    queue_protection=None,
    checks_epoch_at_admission=True,   # one-time epoch compare at dequeue
    checks_key_at_admission=False,
    pins_at_admission=False,          # NO pin -> eviction not blocked
    segment_bytes=None,
    extra_rtt=0,
    protects_transfer=False,          # transfer is unprotected -> mid-xfer race
    queue_reclaim="Y",                # no pin while queued -> reclaimable
))
