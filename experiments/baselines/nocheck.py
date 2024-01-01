"""NoCheck / Unsafe baseline — normalization reference only.

Host enqueues a promotion descriptor; the endpoint does NOT check the epoch,
acquires NO pin, and the descriptor goes straight to the data mover, which
issues the payload.  There is no protection and no validation anywhere, so a
queue-time or transfer-time eviction produces stale payload with no guard.  This
is NOT a candidate correct mechanism; it defines
``normalized_throughput(NoCheck) = 1.0``.
"""
from __future__ import annotations

from .baseline_common import MechanismSpec, register_spec

SPEC = register_spec(MechanismSpec(
    name="NoCheck",
    queue_protection=None,
    checks_epoch_at_admission=False,
    checks_key_at_admission=False,
    pins_at_admission=False,
    segment_bytes=None,
    extra_rtt=0,
    protects_transfer=False,
    queue_reclaim="Y",     # nothing pins the object; endpoint reclaims freely
))
