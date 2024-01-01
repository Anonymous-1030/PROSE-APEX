"""Segmented / cancelable DMA baseline (64 B, 256 B, 4 KiB, 16 KiB).

Built on the RDMA-style generation key, but the payload is split into segments.
Before each segment is committed to the irrevocable payload pipeline the
generation key is re-checked (``segment_check_latency_ns``).  If the check fails,
no further segment is issued; segments already in the in-flight pipeline (up to
``max_inflight_segments``) still complete, and an abort is recorded.  No pin
spanning the whole object transfer is acquired, so the endpoint may evict
between segment boundaries.  Every issued segment pays a header
(``per_segment_header_bytes``) and a sub-descriptor
(``per_segment_descriptor_bytes``), so the control/header overhead grows as the
segment shrinks.

Bounded waste (verified by Test 5):

    wasted_bytes_after_invalidation <= segment_bytes * max_inflight_segments

so smaller segments waste less on invalidation but cost more control/header
overhead — the throughput/waste trade-off swept by the four sizes.

``make_segmented_specs()`` registers one spec per canonical segment size; the
sweep driver imports them all.
"""
from __future__ import annotations

from typing import List

from .baseline_common import MechanismSpec, register_spec, SEGMENT_SIZES


def make_segmented_specs() -> List[MechanismSpec]:
    """Register and return the four segmented/cancelable-DMA specs."""
    specs = []
    for sz in SEGMENT_SIZES:
        specs.append(register_spec(MechanismSpec(
            name=f"Segmented-{sz}",
            queue_protection=None,
            checks_epoch_at_admission=False,
            checks_key_at_admission=True,   # initial admission key check
            pins_at_admission=False,        # no whole-object pin
            segment_bytes=sz,               # per-segment re-check + cancel
            extra_rtt=0,
            protects_transfer=False,        # cancelable, not pinned
            queue_reclaim="Y",
        )))
    return specs


SPECS = make_segmented_specs()
