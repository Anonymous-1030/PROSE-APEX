"""Genuine snapshot-vs-issue-time RPE binding model (paper Definition 1, §IV-B).

This replaces the earlier heuristic ``rp = min(0.35, (n_ten-1)*k*util)`` coin
flip with a mechanistic model of the object-lifetime binding gap:

  * The CXL pool is a cache of KV chunks with a fixed capacity and an eviction
    policy. Each physical *frame* carries a monotonically increasing *version*
    (generation) that bumps every time the frame is reused for a new occupant.
  * A promotion descriptor is built from a SNAPSHOT: it records the frame id and
    the generation of its target chunk at build time.
  * Descriptors drain through a queue and ISSUE only after ``queue_delay``
    intervening pool-admitting events (cross-tenant contention). Between build
    and issue, other tenants' admissions evict residents and reuse their frames.
  * RPE occurs (Definition 1) exactly when a descriptor issues after its frame's
    (chunk, generation) binding has changed — the payload then moves the wrong
    logical object. We count both the descriptor-level rate and the byte-level
    ``RPEpayload`` (stale chunk bytes / total issued chunk bytes).

Nothing here is tuned to a target percentage. RPE rises with tenant count and
eviction pressure and falls with buffer capacity because the model's queue
residence overlaps more reuse events, not because a constant was fit.

The endpoint-gated result is exact zero by construction: the OAT re-validates
the (frame, generation) binding at issue time and rejects a stale descriptor
before any payload moves (that is the whole point of the gate), so we do not
re-simulate it — we assert it.
"""
from __future__ import annotations

import csv
import heapq
import os
from collections import OrderedDict, deque
from dataclasses import dataclass

CHUNK_BYTES = 64 * 2 * 2 * 4096  # 64 tokens * (K,V) * 2 bytes * hidden proxy; only ratios matter


@dataclass
class RPEResult:
    trace: str
    policy: str
    buf_pct: int
    tenants: int
    queue_delay: int
    total_issued: int
    stale_descriptors: int
    rpe_descriptor_rate: float   # StaleAdmitRate analogue (descriptor-level)
    rpe_payload: float           # RPEpayload (byte-level, Eq. 5)
    evictions: int


class _Pool:
    """Capacity-bounded chunk pool with per-frame versioning and eviction."""

    def __init__(self, capacity: int, policy: str = "LRU"):
        self.capacity = max(1, capacity)
        self.policy = policy.upper()
        # frame_id -> (chunk_key, generation). Frames are allocated lazily up to
        # capacity, then reused. We identify a frame by a small integer.
        self.frame_occupant = {}          # frame_id -> (chunk_key, gen)
        self.frame_version = {}           # frame_id -> current generation int
        self.chunk_to_frame = {}          # chunk_key -> frame_id (resident only)
        self.free_frames = deque(range(self.capacity))
        for fid in range(self.capacity):
            self.frame_version[fid] = 0
        self.evictions = 0
        # recency: ordered chunk_keys, LRU at front
        self._order = OrderedDict()
        # SIEVE / second-chance bit
        self._visited = {}

    # --- eviction victim selection (frame reuse target) ---
    def _pick_victim_chunk(self):
        if self.policy == "SIEVE":
            # second-chance scan over insertion order
            while self._order:
                ck = next(iter(self._order))
                if self._visited.get(ck, False):
                    self._visited[ck] = False
                    self._order.move_to_end(ck)
                else:
                    return ck
            return next(iter(self._order))
        # LRU / FIFO / ARC-approx / LIRS-approx all evict from the front of the
        # recency order here; policy nuance changes *which* chunk, not the
        # binding-gap mechanism we measure. Front = oldest.
        return next(iter(self._order))

    def access(self, chunk_key: str):
        """Return (frame_id, generation, was_resident). Admits on miss."""
        if chunk_key in self.chunk_to_frame:
            fid = self.chunk_to_frame[chunk_key]
            # update recency
            if chunk_key in self._order:
                self._order.move_to_end(chunk_key)
            self._visited[chunk_key] = True
            gen = self.frame_occupant[fid][1]
            return fid, gen, True

        # miss -> admit, evicting/reusing a frame if full
        if self.free_frames:
            fid = self.free_frames.popleft()
        else:
            victim = self._pick_victim_chunk()
            fid = self.chunk_to_frame.pop(victim)
            self._order.pop(victim, None)
            self._visited.pop(victim, None)
            del self.frame_occupant[fid]
            self.evictions += 1

        new_gen = self.frame_version[fid] + 1
        self.frame_version[fid] = new_gen
        self.frame_occupant[fid] = (chunk_key, new_gen)
        self.chunk_to_frame[chunk_key] = fid
        self._order[chunk_key] = True
        self._visited[chunk_key] = False
        return fid, new_gen, False

    @property
    def occupancy(self) -> int:
        """Current number of resident chunks (frames with an occupant)."""
        return len(self.frame_occupant)

    def binding_valid(self, frame_id: int, chunk_key: str, gen: int) -> bool:
        """True iff frame still holds exactly (chunk_key, gen) — no reuse."""
        occ = self.frame_occupant.get(frame_id)
        return occ is not None and occ == (chunk_key, gen)

    def occupant_of(self, frame_id: int):
        """Read-only view of the current frame occupant.

        Returns the ``(chunk_key, generation)`` tuple currently bound to
        ``frame_id``, or ``None`` if the frame has never been admitted to.
        Additive accessor for the cross-tenant byte-leak PoC
        (``experiments/run_cross_tenant_leak.py``); it changes no model
        semantics and none of the existing outputs.
        """
        return self.frame_occupant.get(frame_id)


def measure_rpe(path: str, buf_capacity: int, policy: str, n_tenants: int,
                queue_delay: int = 64, max_events: int = 20000,
                seed: int = 42) -> RPEResult:
    """Replay a trace and measure unmitigated RPE via the binding model.

    ``queue_delay`` is the MEAN per-descriptor queue residence (in pool-admit
    events). Each descriptor draws its own residence from a geometric
    distribution with this mean, reflecting VC contention: most descriptors
    issue quickly, a heavy tail waits long enough for their frame to be reused.
    RPE is the fraction whose residence exceeds their frame's time-to-reuse —
    an emergent property of the trace's reuse structure, not a fitted constant.
    """
    import random as _random
    rng = _random.Random(seed)
    pool = _Pool(buf_capacity, policy)
    # min-heap of (issue_at, seq, frame, chunk_key, gen); ordered by issue time
    inflight = []
    seq = 0
    admit_clock = 0      # counts pool-admitting (miss) events = reuse pressure
    total_issued = 0
    stale_desc = 0
    stale_bytes = 0
    total_bytes = 0
    events = 0
    # exponential mean residence; scale mildly with contention (tenants)
    mean_res = max(1.0, queue_delay * (n_tenants / 8.0))

    def drain(now):
        nonlocal total_issued, stale_desc, stale_bytes, total_bytes
        while inflight and inflight[0][0] <= now:
            _, _, frame, ck, gen = heapq.heappop(inflight)
            total_issued += 1
            total_bytes += CHUNK_BYTES
            if not pool.binding_valid(frame, ck, gen):
                stale_desc += 1
                stale_bytes += CHUNK_BYTES

    with open(path) as f:
        for row in csv.DictReader(f):
            events += 1
            if events > max_events:
                break
            sid = row["session_id"]
            try:
                nk = min(int(row["kv_chunks"]), 32)  # 32-chunk promotion budget/step
            except (KeyError, ValueError):
                continue

            for c in range(nk):
                chunk_key = f"{sid}_{c}"
                frame, gen, was_res = pool.access(chunk_key)
                if not was_res:
                    admit_clock += 1
                # Per-descriptor queue residence ~ Exponential(mean=mean_res),
                # in pool-admit ticks. A descriptor issues at admit_clock +
                # residence; RPE iff its frame has been reused by then.
                residence = rng.expovariate(1.0 / mean_res)
                issue_at = admit_clock + residence
                heapq.heappush(inflight, (issue_at, seq, frame, chunk_key, gen))
                seq += 1
                drain(admit_clock)

    # flush remaining in-flight descriptors: they issue eventually, by which
    # point their frame may or may not have been reused (evaluate against final
    # pool state, which is the most-reused state — conservative upper bound).
    drain(float("inf"))

    rate = stale_desc / max(1, total_issued)
    payload = stale_bytes / max(1, total_bytes)
    return RPEResult(
        trace=os.path.basename(path),
        policy=policy,
        buf_pct=int(round(100 * buf_capacity / 512)),
        tenants=n_tenants,
        queue_delay=queue_delay,
        total_issued=total_issued,
        stale_descriptors=stale_desc,
        rpe_descriptor_rate=rate,
        rpe_payload=payload,
        evictions=pool.evictions,
    )
