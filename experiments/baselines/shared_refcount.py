"""TraCT-style shared refcount baseline.

Host atomically increments a shared-object-metadata refcount BEFORE the
promotion descriptor is enqueued.  Because the metadata lives on a non-coherent
path, the increment pays a flush + atomic + visibility latency
(``refcount_op_latency_ns``) and one cacheline of control traffic
(``refcount_op_bytes``).  The endpoint may evict an object only when its
``refcount == 0``; the host decrements after the payload completes (or aborts).

Protection therefore spans host scheduling, queueing AND transfer: the object is
un-reclaimable for the whole request lifetime.  This yields zero RPE by
construction but the widest pin span and blocks queue-time autonomous
reclamation (Q-reclaim = N).
"""
from __future__ import annotations

from .baseline_common import MechanismSpec, register_spec

SPEC = register_spec(MechanismSpec(
    name="SharedRef",
    queue_protection="refcount",   # increment at enqueue, hold across lifetime
    checks_epoch_at_admission=False,
    checks_key_at_admission=False,
    pins_at_admission=False,
    segment_bytes=None,
    # The acquire is NOT free. The refcount lives on a non-coherent shared path,
    # so the increment must complete a serialized flush + remote atomic +
    # visibility fence before the endpoint is guaranteed to observe refcount>0
    # and honor it as an eviction veto. That is one serialized host<->endpoint
    # exchange on the promotion critical path, hence extra_rtt=1, charged at the
    # metadata-atomic latency (refcount_op_latency_ns), which is much smaller
    # than a full reserve RTT but decidedly nonzero. (A truly cache-coherent
    # shared metadata region would make this 0; we do NOT assume that, and the
    # coherence assumption is stated in the audit report.)
    extra_rtt=1,
    serialized_acquire_ns=250.0,    # == BaselineConfig.refcount_op_latency_ns
    protects_transfer=True,         # refcount>0 blocks eviction during transfer
    queue_reclaim="N",              # object pinned from enqueue -> not reclaimable
))
