"""PROSE baseline — fused atomic admission with transfer-lifetime pin.

At descriptor dequeue the endpoint performs, at ONE linearization point:

    if resident(obj) and current_epoch(obj) == descriptor.expected_epoch:
        pin_count[obj] += 1
        admit transfer
    else:
        reject transfer

Epoch validation and pin acquisition share a single linearization point, so no
eviction can be interleaved between them.  Eviction may never pick a
``pin_count > 0`` object.  The pin is released only after the last payload beat
completes (or on explicit abort).  The descriptor is NOT pinned while it is
queued, so the endpoint retains queue-time autonomous reclamation (Q-reclaim =
Y): a stale queued descriptor is simply rejected at admission and issues no
payload.  No host<->endpoint round trip is added (``extra_rtt = 0``).

The runtime invariant checked at every payload issue (Test 6):

    PAYLOAD_ISSUE(d) => resident(obj) and current_epoch == expected_epoch
                        and pin_count(obj) > 0
"""
from __future__ import annotations

from .baseline_common import MechanismSpec, register_spec

SPEC = register_spec(MechanismSpec(
    name="PROSE",
    queue_protection=None,           # NOT pinned while queued -> reclaimable
    checks_epoch_at_admission=True,   # epoch check ...
    checks_key_at_admission=False,
    pins_at_admission=True,           # ... fused with pin at one linearization pt
    segment_bytes=None,
    extra_rtt=0,                      # no extra round trip
    protects_transfer=True,           # pin held admission -> completion
    queue_reclaim="Y",                # queue-time autonomous reclamation retained
))
