"""Experiment B — Page/Block-Cache instance of the commit-time object contract.

GOAL.  Show the contract is not KV-specific: the Object Admission Transaction
(atomic validate-incarnation + acquire transfer-scoped pin) applies verbatim to
an endpoint-managed page/block cache, where the object is a logical page/block,
the incarnation is its reuse epoch, the extent is a cache frame, and the
descriptor is an asynchronous prefetch/read.

This is DESIGN ANALYSIS + CALIBRATED PROJECTION (not a KV measurement): we map
the KV mechanism onto page-cache semantics (table below) and re-parameterize the
SAME mechanistic binding model (trace_adapter/rpe_binding_model.py) with page
access characteristics — 4 KiB pages, prefetch-driven mixed sequential/random
access, an endpoint replacement policy (LRU/ARC), and slot reuse. We report:

  * unmitigated RPE (a stale prefetch moves a reused frame) under the four
    exposure conditions of §II-B;
  * RPE == 0 once the OAT gate validates-and-pins at admission;
  * protection duration vs. a refcount that protects from before enqueue;
  * relative bandwidth efficiency (useful / issued).

The design mapping is the load-bearing contribution; the numbers are a calibrated
projection onto the page-cache regime, labelled as such.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trace_adapter.rpe_binding_model import _Pool  # reuse the frame/gen pool


# ── Design mapping: KV instance  ->  page/block-cache instance ───────────────
DESIGN_MAPPING: List[Tuple[str, str, str]] = [
    # (contract element, KV instance, page/block-cache instance)
    ("Logical object",     "KV chunk (tenant, session, chunk)",
                           "logical page / block (file/dev, offset)"),
    ("Incarnation g",      "generation stamped when chunk (re)enters the pool",
                           "reuse epoch stamped when the frame is (re)filled"),
    ("Extent set S",       "one pool slot (single-extent KV)",
                           "one cache frame (page) or a run of frames (block)"),
    ("Authoritative map",  "endpoint object directory: (id,g) -> slot",
                           "endpoint page table: (page,epoch) -> frame"),
    ("Descriptor d",       "promotion <id,g,slot,len> from a host snapshot",
                           "async prefetch/read <page,epoch,frame,len>"),
    ("Reuse event",        "evict chunk, bump gen, reuse slot for new chunk",
                           "evict page (LRU/ARC), bump epoch, refill frame"),
    ("OAT (admission)",    "atomic: validate g == MAP[id].g, pin (id,g)",
                           "atomic: validate epoch == MAP[page].epoch, pin (page,epoch)"),
    ("RPE",                "payload moves bytes of a reused chunk version",
                           "read/prefetch returns bytes of a reused frame's new page"),
    ("Zero-RPE guarantee", "reject-before-payload; pin blocks reclaim in-flight",
                           "reject-before-read; pin blocks frame reuse in-flight"),
    ("Payload path",       "endpoint DMA (Mode A) / host CXL.mem pull (Mode B)",
                           "endpoint DMA to page buffer / host CXL.mem page read"),
]


def print_mapping() -> str:
    w1 = max(len(a) for a, _, _ in DESIGN_MAPPING)
    lines = ["Contract element  |  KV instance  ->  Page/Block-cache instance", ""]
    for elem, kv, pg in DESIGN_MAPPING:
        lines.append(f"{elem:<{w1}}")
        lines.append(f"    KV  : {kv}")
        lines.append(f"    Page: {pg}")
    return "\n".join(lines)


# ── Page-cache access trace (prefetch-driven, mixed sequential/random) ───────
@dataclass
class PageConfig:
    n_frames: int = 256            # endpoint page-cache frames (extent pool)
    page_bytes: int = 4096         # 4 KiB page
    working_pages: int = 4096      # logical page universe (16x oversubscribed)
    seq_fraction: float = 0.6      # fraction of accesses that are sequential runs
    run_length: int = 16          # pages per sequential prefetch run
    n_accesses: int = 40000
    mean_queue_residence: int = 128  # prefetch descriptor queue residence (admits)
    policy: str = "LRU"
    seed: int = 7


def gen_page_trace(cfg: PageConfig) -> np.ndarray:
    """Generate a page-access sequence: sequential prefetch runs interleaved with
    random hot-page reads (a realistic page-cache / block-prefetch pattern)."""
    rng = np.random.default_rng(cfg.seed)
    out = np.empty(cfg.n_accesses, dtype=np.int64)
    i = 0
    while i < cfg.n_accesses:
        if rng.random() < cfg.seq_fraction:
            start = int(rng.integers(0, cfg.working_pages))
            for k in range(cfg.run_length):
                if i >= cfg.n_accesses:
                    break
                out[i] = (start + k) % cfg.working_pages
                i += 1
        else:
            # random hot page (Zipf-skewed)
            out[i] = int(rng.zipf(1.2) % cfg.working_pages)
            i += 1
    return out


def measure_page_rpe(cfg: PageConfig, gated: bool) -> Dict:
    """Replay the page trace through the frame/incarnation pool.

    `gated=False`: unmitigated — a prefetch descriptor built from a snapshot
    issues after `mean_queue_residence` admits; if its frame was reused meanwhile
    (epoch bump), it moves the wrong page -> RPE.
    `gated=True` : OAT re-validates the (frame, epoch) at issue and rejects a
    stale prefetch before any read -> RPE == 0; a transfer-scoped pin also blocks
    frame reuse for the duration.
    """
    rng = np.random.default_rng(cfg.seed + (1 if gated else 0))
    pool = _Pool(cfg.n_frames, cfg.policy)
    trace = gen_page_trace(cfg)

    import heapq
    inflight: List[tuple] = []
    admit_clock = 0
    total_issued = 0
    stale = 0
    total_bytes = 0.0
    stale_bytes = 0.0
    rejected = 0
    seq = 0
    mean_res = max(1.0, float(cfg.mean_queue_residence))

    def drain(now: float):
        nonlocal total_issued, stale, total_bytes, stale_bytes, rejected
        while inflight and inflight[0][0] <= now:
            _, _, frame, page, epoch = heapq.heappop(inflight)
            valid = pool.binding_valid(frame, page, epoch)
            if gated:
                # OAT re-validates at issue: a stale prefetch is rejected with a
                # null completion — no page bytes move.
                if valid:
                    total_issued += 1
                    total_bytes += cfg.page_bytes
                else:
                    rejected += 1
            else:
                total_issued += 1
                total_bytes += cfg.page_bytes
                if not valid:
                    stale += 1
                    stale_bytes += cfg.page_bytes

    for page in trace:
        page = int(page)
        frame, epoch, was_res = pool.access(f"p{page}")
        if not was_res:
            admit_clock += 1
        residence = rng.exponential(mean_res)
        heapq.heappush(inflight, (admit_clock + residence, seq, frame, f"p{page}", epoch))
        seq += 1
        drain(admit_clock)
    drain(float("inf"))

    denom = total_issued + (stale if not gated else 0)
    issued_bytes = total_bytes
    rpe_payload = (stale_bytes / issued_bytes) if issued_bytes > 0 else 0.0
    # bandwidth efficiency: useful (non-stale) bytes / issued bytes
    useful_bytes = issued_bytes - stale_bytes
    bw_eff = useful_bytes / issued_bytes if issued_bytes > 0 else 1.0

    return {
        "policy": cfg.policy,
        "gated": gated,
        "issued_reads": total_issued,
        "stale_reads": stale,
        "rejected_reads": rejected,
        "rpe_payload_frac": rpe_payload,
        "bandwidth_efficiency": bw_eff,
        "evictions": pool.evictions,
    }


def protection_duration_ratio(cfg: PageConfig) -> Dict:
    """Protection-span ratio (protected interval / transfer interval) for the
    page-cache instance, mirroring §IV-C.

      * PROSE  : pin acquired at admission, released at read completion -> span
                 == transfer, ratio 1.0.
      * REFCNT : refcount acquired before the prefetch is enqueued, released
                 after completion -> span == queue_residence + transfer.

    Transfer time == page serialization; queue residence == mean_queue_residence
    admit-ticks scaled to the same time base."""
    # per-page transfer time (ns) at a representative page-cache CXL bandwidth
    link_gbps = 8.0
    transfer_ns = cfg.page_bytes / link_gbps
    # each admit-tick ~ one page transfer on the shared link
    residence_ns = cfg.mean_queue_residence * transfer_ns
    prose_span = transfer_ns
    refcnt_span = residence_ns + transfer_ns
    return {
        "prose_span_ratio": prose_span / transfer_ns,
        "refcnt_span_ratio": refcnt_span / transfer_ns,
        "transfer_ns": transfer_ns,
        "residence_ns": residence_ns,
    }
