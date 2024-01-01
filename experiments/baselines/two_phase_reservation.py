"""Two-phase endpoint reservation baseline.

Explicit two-phase protocol:

  Phase 1:  HOST -> ENDPOINT: RESERVE(object_id, epoch)
            endpoint atomically {check resident, check epoch, acquire
            reservation pin, return unique token}.
  Phase 2:  HOST -> ENDPOINT: TRANSFER(token, dst, len); on completion the
            endpoint releases the reservation.

The reserve response must return before the host issues the payload request, so
the token-exchange round-trip (``reserve_rtt_ns``) is prepended to the critical
path (``extra_rtt = 1``).  The reservation pin is held from the instant the
endpoint accepts the reserve until payload completion, so the object cannot be
reclaimed while the request is queued (Q-reclaim = N) and RPE is zero by
construction.  The token is bound to the slot incarnation, so a slot reused
after the reserve invalidates the token (no stale payload).
"""
from __future__ import annotations

from .baseline_common import MechanismSpec, register_spec

SPEC = register_spec(MechanismSpec(
    name="TwoPhase",
    queue_protection="reserve",     # reserve pin acquired at phase 1, held to done
    checks_epoch_at_admission=True,  # reserve checks residency + epoch atomically
    checks_key_at_admission=False,
    pins_at_admission=False,         # pin already held from the reserve phase
    segment_bytes=None,
    extra_rtt=1,                     # the token-exchange round-trip
    serialized_acquire_ns=3500.0,    # == BaselineConfig.reserve_rtt_ns
    protects_transfer=True,
    queue_reclaim="N",               # reservation held from phase 1 -> not reclaimable
))
