"""Generation-check + epoch-fence baseline (Tigon-style EBR).

Identical to GenOnly at the endpoint: the descriptor carries ``object_id`` and
``expected_epoch``; at admission the current mapping is read and the transfer
is allowed iff the object is resident and the epoch matches.  The check does
NOT increment a pin count and does NOT block eviction — no transfer-span hold.

The one difference is on the RECLAIM path.  When the placement authority wants
to reclaim/overwrite a slot, the reclaim is deferred by one epoch grace period
(epoch-based reclamation, Tigon-style):

  * the UNLINK (epoch bump + slot-key rotation — what a fresh admission check
    observes) takes effect immediately, so a descriptor that dequeues after
    the reclaim request is rejected exactly as under GenOnly;
  * the slot OVERWRITE — the moment an already-admitted transfer's payload
    reads turn stale — takes effect only after one grace period has elapsed
    since the request.  Bytes issued inside the grace window still read the
    authorized generation.

Grace-period value: ONE ALLOCATOR EPOCH, ``cfg.eviction_interval_ns`` (500 ns
nominal / 250 ns race-stress) — the workload's natural reclamation timescale,
i.e. the epoch of the reclamation protocol itself.  Alternatives considered
and rejected: one decode step (1 ms, the design-space harness's epoch) or one
full transfer window (16.4 us for a 64 KiB object at 4 GB/s) would cover every
transfer end-to-end, trivially zeroing the exposure — a fence modeled longer
than intended for a grace period (the baseline would measure the fence length,
not the mechanism).

This baseline demonstrates that deferring reuse by one grace period is NOT a
substitute for a transfer-span pin: a descriptor that dequeued before the
fence expires still runs unprotected after it expires, so the post-fence tail
of a raced transfer is stale.  The fence can only shrink the exposure window
(<= GenOnly at every point); it never enlarges it, because the unlink is
visible to admission checks immediately.
"""
from __future__ import annotations

from .baseline_common import MechanismSpec, register_spec

SPEC = register_spec(MechanismSpec(
    name="GenOnlyEpochFence",
    queue_protection=None,
    checks_epoch_at_admission=True,   # one-time epoch compare at dequeue (== GenOnly)
    checks_key_at_admission=False,
    pins_at_admission=False,          # NO pin -> eviction not blocked
    segment_bytes=None,
    extra_rtt=0,
    protects_transfer=False,          # transfer is unprotected -> mid-xfer race
    queue_reclaim="Y",                # no pin while queued -> reclaimable
    epoch_fence=True,                 # slot overwrite deferred one allocator epoch
))
