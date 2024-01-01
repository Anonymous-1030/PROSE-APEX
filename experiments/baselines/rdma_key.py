"""RDMA-style generation-capability baseline.

Every reusable slot carries a ``slot_key`` (generation key), regenerated each
time the slot is allocated to a new logical object (slot reuse rotates the key
immediately).  The descriptor carries the expected key.  The endpoint checks the
key when the descriptor is accepted by the payload engine; the transfer starts
only if the key matches.  No object-level pin is acquired, and payload beats that
already passed the check and entered the pipeline are not auto-revoked.

The capability rejects a stale descriptor that has NOT yet started (queue-time
slot reuse rotates the key -> reject, no payload).  But a single admission-time
check cannot protect a long transfer whose slot is reused AFTER the check
passes: the non-preemptible transfer keeps issuing post-eviction bytes, which
are stale.  This is deliberately checked ONCE (at descriptor/payload admission)
so it is distinct from the per-segment cancelable-DMA baseline.
"""
from __future__ import annotations

from .baseline_common import MechanismSpec, register_spec

SPEC = register_spec(MechanismSpec(
    name="RDMAKey",
    queue_protection=None,
    checks_epoch_at_admission=False,
    checks_key_at_admission=True,     # single slot-key capability check
    pins_at_admission=False,          # capability != pin: slot not locked
    segment_bytes=None,               # single check, whole-object transfer
    extra_rtt=0,
    protects_transfer=False,          # one check cannot span the transfer
    queue_reclaim="Y",
))
