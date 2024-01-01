"""Two-phase (reserve-then-pull) admission baseline for CXL KV-cache promotion.

This module implements ``TwoPhaseValidationBaseline``, a *strong, fair*
competitor to the single-phase endpoint gate (CEFE / PROSE-APEX). It is the
concrete, parameterised realisation of the "versioned chunk headers with a
reserve-then-pull protocol" discussed in the paper's Background: it genuinely
eliminates Reclaimed-Payload Exposure (RPE == 0 by construction), uses the same
64 B metadata summary as every other score-before-fetch boundary, and admits
exactly the same byte-efficient set. It differs from CEFE on one axis only:
each admission requires a **round-trip reservation exchange** before any payload
moves, and the issued capability token is *pinned* at the endpoint for the whole
round-trip.

Why that one difference is decisive under load (Little's law)
-------------------------------------------------------------
The number of reservations simultaneously held at the endpoint is

    L = lambda_reserve * RTT_reserve                      (Little's law)

where ``lambda_reserve`` is the batch/descriptor reserve arrival rate across all
tenants and ``RTT_reserve`` is the 2-5 us token-exchange round-trip. Because the
round-trip is ~300-500x the single-phase admit decision (9 ns), even a modest
arrival rate drives ``L`` past the bounded reservation (token) table. Once the
table is full, the endpoint *must* back-pressure the reserve queue: a new batch
cannot reserve until enough in-flight reservations drain. That explicit stall
(``wait_ns``) is the physical root of the P99 blow-up under high concurrency --
not a mere additive RTT. CEFE fuses validation and commit into a single local
atomic decision at descriptor dequeue, so it holds no reservation across a
round-trip: ``L`` stays ~pipeline-depth and the tail stays flat.

The byte-level CXL.mem timing (flit serialization, DRAM row hit/miss, M/D/1
queuing, bandwidth contention) is delegated **unchanged** to the shared
``CXLQueueSimulator`` so this baseline sees identical hardware constraints as
every other policy in the comparison.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cxl_queue_simulator import CXLQueueSimulator, CXLQueueConfig, StepStats

NS_PER_US = 1_000.0


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class TwoPhaseConfig:
    """Parameters of the reserve-then-pull protocol.

    All costs are on top of the shared ``CXLQueueSimulator`` byte-level model;
    this config only adds the *protocol-level* reservation overheads.
    """
    # Token-exchange round-trip (host -> endpoint reserve -> host token). The
    # paper quotes +2-5 us/batch; default sits mid-range and is swept by the
    # driver. This RTT is charged to the critical path AND is the window over
    # which a token slot stays pinned (Little's law occupancy).
    reserve_rtt_us: float = 3.5
    # Reserve request carries chunk-id / epoch / namespace (same 64 B summary
    # the other score-before-fetch boundaries read).
    reserve_meta_bytes: int = 64
    # Capability token returned by the endpoint (HMAC-SHA256 truncated).
    token_bytes: int = 32
    # Bounded outstanding-reservation (token) table at the endpoint. This is the
    # capacity that Little's-law occupancy saturates under oversubscription.
    token_table_capacity: int = 256
    # Reservation validity window; a token unredeemed past this is expired and
    # its pull is rejected (no payload -> RPE stays 0).
    token_expiry_us: float = 50.0
    # Per-reserve endpoint decision (residency check + pin install), mirrors the
    # 9 ns CEFE admit so the *decision* cost is not what differentiates them.
    endpoint_reserve_service_ns: float = 9.0
    # Re-validate the token (epoch / nonce) at pull-service time. This is what
    # structurally guarantees RPE == 0 for expired reservations.
    verify_on_pull: bool = True


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class TwoPhaseBatchResult:
    """End-to-end result of one host's batch through the two-phase protocol."""
    admitted_ids: List[int] = field(default_factory=list)
    n_reserved: int = 0
    n_expired: int = 0
    # Latency decomposition (ns), all on the same critical path (phases serial).
    reserve_wait_ns: float = 0.0     # back-pressure stall waiting for token slots
    reserve_rtt_ns: float = 0.0      # the token-exchange round-trip
    reserve_meta_ns: float = 0.0     # CXL.mem metadata read (shared backend)
    pull_payload_ns: float = 0.0     # payload transfer (shared backend)
    total_lat_ns: float = 0.0        # reserve_wait + reserve_rtt + meta + pull
    # Correctness / efficiency
    rpe_bytes: float = 0.0           # MUST be 0 (structural guarantee)
    payload_bytes: int = 0
    # Endpoint occupancy observed *after* this batch pinned its tokens.
    outstanding_after: int = 0

    @property
    def total_lat_us(self) -> float:
        return self.total_lat_ns / NS_PER_US


# --------------------------------------------------------------------------- #
# Two-phase baseline                                                          #
# --------------------------------------------------------------------------- #
class TwoPhaseValidationBaseline:
    """Reserve-then-pull admission competitor sharing the CXL byte-level model.

    Usage (per-batch driving, mirrors ``BaselineCXLSession`` shape)::

        cxl  = CXLQueueSimulator(make_cxl_asic_config())
        tp   = TwoPhaseValidationBaseline(TwoPhaseConfig(), cxl)
        res  = tp.submit_batch(candidate_ids, budget, arrival_time_ns)
        ...
        stats = tp.end_step()

    The reservation table is modelled as a list of ``(release_time_ns)`` pins.
    ``outstanding_reservations(t)`` returns the live count -- the empirical
    ``L`` that Little's law predicts as ``lambda * RTT``.
    """

    def __init__(
        self,
        cfg: Optional[TwoPhaseConfig] = None,
        cxl: Optional[CXLQueueSimulator] = None,
        scorer_fn=None,
    ) -> None:
        self.cfg = cfg or TwoPhaseConfig()
        self.cxl = cxl or CXLQueueSimulator()
        # Optional ranking function: List[int] -> ranked List[int]. If None, the
        # candidate order is treated as already score-ranked (fair: identical
        # to what CEFE's scorer would produce -- we isolate placement, not rank).
        self.scorer_fn = scorer_fn
        # Reservation (token) table: each entry is the wall-clock ns at which the
        # pinned token is released (issue_time + RTT). Sorted-on-demand.
        self._pins: List[float] = []
        # Diagnostics
        self.total_reserved = 0
        self.total_expired = 0
        self.total_backpressure_stalls = 0
        self.total_wait_ns = 0.0
        self.peak_outstanding = 0

    # ---- reservation-table bookkeeping ------------------------------------
    def _expire_pins(self, t_now_ns: float) -> None:
        """Release every token whose round-trip has completed by ``t_now_ns``.

        A pin installed at issue time is held for exactly one ``reserve_rtt``;
        the stored value is its release time, so a pin is live iff
        ``release_time > t_now``. This is the discrete realisation of the
        occupancy window in ``L = lambda * RTT``.
        """
        self._pins = [rel for rel in self._pins if rel > t_now_ns]

    def outstanding_reservations(self, t_now_ns: float) -> int:
        """Live pinned-token count at ``t_now_ns`` (the empirical ``L``)."""
        return sum(1 for rel in self._pins if rel > t_now_ns)

    def _time_until_k_slots_free(self, k_needed: int, t_now_ns: float) -> float:
        """Back-pressure wait: ns until ``k_needed`` token slots become free.

        With the table full, slots free only as in-flight reservations drain,
        i.e. as pinned tokens reach their release times. To free ``k_needed``
        slots we must wait until the ``k_needed``-th soonest release. That
        release-time offset from *now* is exactly the stall the endpoint injects
        into the reserve path -- the ``wait_ns`` whose growth under rising
        ``lambda`` (Little's law) produces the P99 blow-up.
        """
        live = sorted(rel for rel in self._pins if rel > t_now_ns)
        if k_needed <= 0:
            return 0.0
        if k_needed > len(live):
            # Cannot free enough even by draining every live pin: wait for the
            # last one (all slots) -- the batch is fully serialized behind the
            # table. (Bounded by construction: k_needed <= table capacity.)
            idx = len(live) - 1
        else:
            idx = k_needed - 1
        if idx < 0:
            return 0.0
        return max(0.0, live[idx] - t_now_ns)

    # ---- phase 1: reserve --------------------------------------------------
    def _reserve_phase(
        self, candidate_ids: List[int], t_now_ns: float
    ) -> Tuple[List[int], float, float, float, int]:
        """Reserve tokens for ``candidate_ids``.

        Returns ``(reserved_ids, wait_ns, meta_ns, rtt_ns, outstanding_after)``.

        Steps:
          1. Expire pins whose RTT elapsed (free their slots).
          2. If free slots < requested, BACK-PRESSURE: stall ``wait_ns`` until
             enough in-flight reservations drain (Little's-law saturation).
          3. Read the 64 B metadata summary over the shared CXL.mem backend.
          4. Pin a token per reserved chunk for the whole RTT.
        """
        cfg = self.cfg
        self._expire_pins(t_now_ns)

        n_req = len(candidate_ids)
        free = cfg.token_table_capacity - len(self._pins)

        # --- (2) explicit back-pressure stall when the token table saturates --
        wait_ns = 0.0
        if free < n_req:
            k_needed = n_req - free
            wait_ns = self._time_until_k_slots_free(k_needed, t_now_ns)
            self.total_backpressure_stalls += 1
            self.total_wait_ns += wait_ns
            # After the stall, those slots are genuinely free: advance and expire.
            self._expire_pins(t_now_ns + wait_ns)

        t_after_wait = t_now_ns + wait_ns

        # --- (3) reserve metadata read on the shared byte-level CXL backend ----
        meta_res = self.cxl.submit_summary_fetch(candidate_ids, t_after_wait)
        meta_ns = meta_res.total_ns

        # --- (4) pin one token per reserved chunk for the full RTT -------------
        rtt_ns = cfg.reserve_rtt_us * NS_PER_US
        release_time = t_after_wait + meta_ns + rtt_ns
        for _ in candidate_ids:
            self._pins.append(release_time)
        self.total_reserved += n_req

        outstanding_after = len(self._pins)
        self.peak_outstanding = max(self.peak_outstanding, outstanding_after)

        return list(candidate_ids), wait_ns, meta_ns, rtt_ns, outstanding_after

    # ---- phase 2: pull -----------------------------------------------------
    def _pull_phase(
        self,
        reserved_ids: List[int],
        budget: int,
        t_pull_ns: float,
        reserve_issue_ns: float,
    ) -> Tuple[List[int], float, int, int]:
        """Redeem tokens and pull payload for the admitted top-``budget`` set.

        Returns ``(admitted_ids, payload_ns, payload_bytes, n_expired)``.

        A token unredeemed past ``token_expiry`` is rejected at pull-service
        time: its read is dropped and NO payload moves, so RPE stays exactly 0.
        Under normal operation expiry is rare; it is the *correctness* guard,
        never a source of wasted payload.
        """
        cfg = self.cfg
        expiry_ns = cfg.token_expiry_us * NS_PER_US

        # (1) re-validate: drop tokens whose validity window elapsed pre-pull.
        if cfg.verify_on_pull:
            valid = [c for c in reserved_ids
                     if (t_pull_ns - reserve_issue_ns) <= expiry_ns]
        else:
            valid = list(reserved_ids)
        n_expired = len(reserved_ids) - len(valid)
        self.total_expired += n_expired

        # (2) rank and keep only the budget (same scorer as CEFE -> fair).
        ranked = self.scorer_fn(valid) if self.scorer_fn is not None else valid
        admitted = ranked[:budget]

        # (3) pull payload for admitted set over the shared byte-level backend.
        payload_res = self.cxl.submit_payload_fetch(admitted, t_pull_ns)
        self.cxl.mark_chunks_used(admitted)

        return admitted, payload_res.total_ns, payload_res.total_bytes, n_expired

    # ---- public per-batch API ---------------------------------------------
    def submit_batch(
        self, candidate_ids: List[int], budget: int, arrival_time_ns: float
    ) -> TwoPhaseBatchResult:
        """Drive one host's batch through reserve-then-pull.

        The two phases are SERIAL on the critical path: the host cannot pull
        payload until it holds valid tokens, so the reservation round-trip is
        prepended to (not overlapped with) the payload transfer.
        """
        reserved, wait_ns, meta_ns, rtt_ns, outstanding_after = self._reserve_phase(
            candidate_ids, arrival_time_ns
        )

        # Reservation completes after wait + metadata + one round-trip.
        reserve_issue_ns = arrival_time_ns + wait_ns + meta_ns
        t_pull_ns = reserve_issue_ns + rtt_ns

        admitted, payload_ns, payload_bytes, n_expired = self._pull_phase(
            reserved, budget, t_pull_ns, reserve_issue_ns
        )

        total_lat_ns = wait_ns + meta_ns + rtt_ns + payload_ns

        return TwoPhaseBatchResult(
            admitted_ids=admitted,
            n_reserved=len(reserved),
            n_expired=n_expired,
            reserve_wait_ns=wait_ns,
            reserve_rtt_ns=rtt_ns,
            reserve_meta_ns=meta_ns,
            pull_payload_ns=payload_ns,
            total_lat_ns=total_lat_ns,
            rpe_bytes=0.0,               # structural: no pull without a valid token
            payload_bytes=payload_bytes,
            outstanding_after=outstanding_after,
        )

    def end_step(self) -> StepStats:
        """Finalise per-step CXL accounting via the shared backend."""
        return self.cxl.end_step()

    def reset(self) -> None:
        """Clear reservation table and diagnostics for a fresh run."""
        self._pins.clear()
        self.total_reserved = 0
        self.total_expired = 0
        self.total_backpressure_stalls = 0
        self.total_wait_ns = 0.0
        self.peak_outstanding = 0
        self.cxl.reset()
