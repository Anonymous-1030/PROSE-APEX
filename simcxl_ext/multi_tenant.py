"""Multi-tenant KV cache promotion management for shared GPU + CXL memory pools.

When multiple LLM inference requests share the same GPU and CXL memory pool,
promotion bandwidth becomes a shared resource that requires fair allocation.
This module models contention, fairness, and SLO-aware scheduling for the
HPCA paper's multi-tenant evaluation scenario.

Key abstractions:
  - FairPromotionAllocator: bandwidth allocation across K tenants
  - PromotionContentionModel: CXL link contention and queuing
  - SLOAwareScheduler: per-token latency SLO enforcement
  - MultiTenantSimulator: end-to-end K-tenant decode simulation
  - FairnessMetrics: Jain's index, starvation detection, utilization
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================

class AllocationPolicy(str, Enum):
    """Bandwidth allocation policy for multi-tenant promotion."""
    EQUAL_SHARE = "equal_share"
    PROPORTIONAL_UTILITY = "proportional_utility"
    MAX_MIN_FAIRNESS = "max_min_fairness"


# =============================================================================
# Hardware-Aligned Utility Functions
# =============================================================================

# VC-WRR weight granularity: 4-bit unsigned (0..15), matching RTL WEIGHT_W=4
VC_WRR_WEIGHT_BITS = 4
VC_WRR_MAX_WEIGHT = (1 << VC_WRR_WEIGHT_BITS) - 1  # 15


def quantize_priority_to_4bit(priority: float, max_priority: float = 1.0) -> int:
    """Quantize a floating-point priority to 4-bit hardware weight (0..15).

    This matches the RTL's WEIGHT_W=4 parameter in both cefe_vc_wrr.sv and
    the standalone APEX_VC_WRR.sv, ensuring the Python simulation reflects
    the same fairness granularity as the hardware.

    Args:
        priority: floating-point priority in [0, max_priority].
        max_priority: the maximum expected priority value.

    Returns:
        Integer weight in [1, 15]. Floor of 1 prevents complete starvation
        (matching RTL behavior where weight=0 means VC is inactive).
    """
    if max_priority <= 0:
        return 1
    normalized = min(1.0, max(0.0, priority / max_priority))
    weight = int(normalized * VC_WRR_MAX_WEIGHT)
    return max(1, min(VC_WRR_MAX_WEIGHT, weight))


# =============================================================================
# Data Types
# =============================================================================

@dataclass
class TenantState:
    """Runtime state for a single tenant sharing the GPU + CXL pool."""

    tenant_id: str
    model_name: str
    context_length: int
    kv_budget_bytes: int
    current_hbm_usage: int = 0
    current_tail_size: int = 0
    promotion_history: List[Tuple[float, int, float]] = field(default_factory=list)
    priority: float = 1.0
    slo_target_latency_us: float = 500.0

    def recent_utility(self, window: int = 10) -> float:
        """Average utility gained over the last *window* promotions."""
        if not self.promotion_history:
            return 0.0
        recent = self.promotion_history[-window:]
        return sum(u for _, _, u in recent) / len(recent)

    def recent_bytes(self, window: int = 10) -> int:
        """Total bytes promoted over the last *window* promotions."""
        if not self.promotion_history:
            return 0
        return sum(b for _, b, _ in self.promotion_history[-window:])


@dataclass
class PromotionRequestEntry:
    """A single tenant's promotion request for one decode step."""

    tenant_id: str
    requested_bytes: int
    expected_utility: float
    priority: float = 1.0
    chunk_count: int = 1


@dataclass
class ContentionEvent:
    """Record of a contention event on the shared CXL link."""

    step: int
    tenant_ids: List[str]
    total_requested_bytes: int
    available_bandwidth_bytes: int
    queuing_delay_us: float
    hol_blocking_us: float


@dataclass
class StarvationEvent:
    """Record of a tenant being starved of promotion bandwidth."""

    tenant_id: str
    start_step: int
    end_step: int
    duration_steps: int
    fair_share_fraction: float


@dataclass
class ApprovedPromotion:
    """A promotion request approved by the SLO scheduler."""

    tenant_id: str
    approved_bytes: int
    expected_latency_us: float
    deferred: bool = False


@dataclass
class MultiTenantResult:
    """Aggregated results from a multi-tenant simulation run."""

    per_tenant_throughput: Dict[str, float] = field(default_factory=dict)
    per_tenant_latency: Dict[str, float] = field(default_factory=dict)
    per_tenant_utility: Dict[str, float] = field(default_factory=dict)
    fairness_index: float = 0.0
    bandwidth_utilization: float = 0.0
    slo_violations_per_tenant: Dict[str, int] = field(default_factory=dict)
    contention_events: List[ContentionEvent] = field(default_factory=list)
    starvation_events: List[StarvationEvent] = field(default_factory=list)
    allocation_history: List[Dict[str, int]] = field(default_factory=list)


# =============================================================================
# FairPromotionAllocator
# =============================================================================

class FairPromotionAllocator:
    """Allocates shared CXL promotion bandwidth across K tenants.

    Three policies are supported:
      1. Equal Share:  B_tenant = B_total / K
      2. Proportional to Utility:  B_tenant = B_total * (U_i / sum(U))
      3. Max-Min Fairness:  iterative water-filling to maximise the
         minimum per-tenant utility.
    """

    def __init__(self) -> None:
        self.allocation_history: List[Dict[str, int]] = []

    def allocate(
        self,
        tenants: List[TenantState],
        total_bandwidth_bytes: int,
        policy: AllocationPolicy = AllocationPolicy.EQUAL_SHARE,
    ) -> Dict[str, int]:
        """Return per-tenant bandwidth allocation in bytes.

        Args:
            tenants: current state of every tenant.
            total_bandwidth_bytes: total promotion bandwidth available
                this step (bytes per step interval).
            policy: which allocation strategy to use.

        Returns:
            Mapping of tenant_id -> allocated bytes.
        """
        if not tenants:
            return {}

        if policy == AllocationPolicy.EQUAL_SHARE:
            alloc = self._equal_share(tenants, total_bandwidth_bytes)
        elif policy == AllocationPolicy.PROPORTIONAL_UTILITY:
            alloc = self._proportional_utility(tenants, total_bandwidth_bytes)
        elif policy == AllocationPolicy.MAX_MIN_FAIRNESS:
            alloc = self._max_min_fairness(tenants, total_bandwidth_bytes)
        else:
            raise ValueError(f"Unknown allocation policy: {policy}")

        self.allocation_history.append(alloc)
        return alloc

    # -----------------------------------------------------------------
    # Policy implementations
    # -----------------------------------------------------------------

    @staticmethod
    def _equal_share(
        tenants: List[TenantState],
        total_bandwidth_bytes: int,
    ) -> Dict[str, int]:
        """B_tenant = B_total / K."""
        k = len(tenants)
        per_tenant = total_bandwidth_bytes // k
        return {t.tenant_id: per_tenant for t in tenants}

    @staticmethod
    def _proportional_utility(
        tenants: List[TenantState],
        total_bandwidth_bytes: int,
    ) -> Dict[str, int]:
        """B_tenant = B_total * (U_tenant / sum(U_i)).

        Tenants with zero recent utility receive a small floor allocation
        (1 / (10 * K) of total) to avoid complete starvation.
        """
        k = len(tenants)
        utilities = {t.tenant_id: t.recent_utility() for t in tenants}
        total_u = sum(utilities.values())

        if total_u <= 0:
            per_tenant = total_bandwidth_bytes // k
            return {t.tenant_id: per_tenant for t in tenants}

        floor = max(1, total_bandwidth_bytes // (10 * k))
        distributable = total_bandwidth_bytes - floor * k

        alloc: Dict[str, int] = {}
        for t in tenants:
            share = int(distributable * (utilities[t.tenant_id] / total_u))
            alloc[t.tenant_id] = floor + share
        return alloc

    @staticmethod
    def _max_min_fairness(
        tenants: List[TenantState],
        total_bandwidth_bytes: int,
        max_iterations: int = 50,
    ) -> Dict[str, int]:
        """Iterative water-filling to maximise the minimum per-tenant utility.

        Each iteration raises the water level uniformly.  Tenants whose
        demand is already satisfied are frozen and their excess is
        redistributed to the remaining tenants.
        """
        k = len(tenants)
        demands = {t.tenant_id: t.current_tail_size for t in tenants}
        alloc: Dict[str, int] = {t.tenant_id: 0 for t in tenants}
        remaining = total_bandwidth_bytes
        active_ids = [t.tenant_id for t in tenants]

        for _ in range(max_iterations):
            if not active_ids or remaining <= 0:
                break

            water = remaining // len(active_ids)
            if water <= 0:
                break

            next_active: List[str] = []
            for tid in active_ids:
                need = max(0, demands[tid] - alloc[tid])
                grant = min(water, need)
                alloc[tid] += grant
                remaining -= grant
                if alloc[tid] < demands[tid]:
                    next_active.append(tid)

            if len(next_active) == len(active_ids):
                break
            active_ids = next_active

        # Distribute any leftover evenly among all tenants.
        if remaining > 0 and k > 0:
            extra = remaining // k
            for tid in alloc:
                alloc[tid] += extra

        return alloc


# =============================================================================
# PromotionContentionModel
# =============================================================================

class PromotionContentionModel:
    """Models contention when multiple tenants issue promotions simultaneously.

    Effects captured:
      - CXL link queuing: requests queue at the memory controller.
      - Head-of-line (HOL) blocking: a large transfer delays smaller ones.
      - Priority queuing: SLO-aware scheduling reorders the queue.
    """

    def __init__(
        self,
        controller_queue_depth: int = 16,
        min_transfer_granularity_bytes: int = 4096,
    ) -> None:
        self.controller_queue_depth = controller_queue_depth
        self.min_transfer_granularity_bytes = min_transfer_granularity_bytes

    def simulate_contention(
        self,
        requests: List[PromotionRequestEntry],
        link_bandwidth_gbps: float = 64.0,
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Simulate contention for a single step.

        Args:
            requests: per-tenant promotion requests.
            link_bandwidth_gbps: raw CXL link bandwidth in GB/s.

        Returns:
            (effective_bw, latency) -- both dicts keyed by tenant_id.
            effective_bw is in bytes/us, latency is in microseconds.
        """
        if not requests:
            return {}, {}

        link_bytes_per_us = link_bandwidth_gbps * 1e9 / 1e6  # GB/s -> B/us

        # Sort by priority descending (higher priority served first).
        sorted_reqs = sorted(requests, key=lambda r: r.priority, reverse=True)

        effective_bw: Dict[str, float] = {}
        latency: Dict[str, float] = {}
        queue_position_us = 0.0

        for req in sorted_reqs:
            transfer_bytes = max(req.requested_bytes, self.min_transfer_granularity_bytes)
            transfer_time_us = transfer_bytes / link_bytes_per_us

            # Queuing delay = time waiting for earlier transfers to finish.
            queuing_delay = queue_position_us

            # HOL blocking: if this request is small but stuck behind a large
            # one, the queuing delay already captures that effect.
            total_latency = queuing_delay + transfer_time_us
            eff_bw = (
                transfer_bytes / total_latency if total_latency > 0
                else link_bytes_per_us
            )

            effective_bw[req.tenant_id] = eff_bw
            latency[req.tenant_id] = total_latency

            # Advance queue position for the next tenant.
            queue_position_us += transfer_time_us

        return effective_bw, latency


# =============================================================================
# CFOCoalesceModel
# =============================================================================

class CFOCoalesceModel:
    """Models the 16-entry CAM Coalesced Fetch-Once (CFO) deduplication.

    When multiple tenants request the same CXL-resident chunk within a decode
    step, the CFO CAM detects the overlap via a 64-bit HMAC-SHA256 truncated
    tag equality check and issues only a single DMA read.  The payload is then
    fanned out to all requesting tenants.

    This reduces effective CXL link traffic proportionally to the cross-tenant
    overlap ratio, matching the RTL implementation in cefe_cfo_cam.sv.

    Hardware correspondence:
      - 16 CAM entries (NUM_ENTRIES=16 in RTL)
      - 64-bit tag comparison (single-cycle equality in RTL)
      - LRU eviction when CAM is full
      - Dynamic clock gating when hit rate < 0.45 for 8 consecutive steps
    """

    def __init__(self, cam_entries: int = 16) -> None:
        self.cam_entries = cam_entries
        # CAM state: chunk_id -> {tenants, hmac_tag, access_order}
        self.cam: Dict[int, dict] = {}
        self._access_counter = 0

        # Statistics per step (mirrors RTL step_hits / step_total counters)
        self.step_hits = 0
        self.step_total = 0

        # EMA hit rate tracking (Q0.8 fixed-point, mirroring RTL)
        self._ema_hit_rate = 128  # 0.5 initial (neutral)
        self._low_rate_counter = 0
        self.cam_gated = False

        # Cumulative statistics
        self.total_dma_saved = 0
        self.total_requests = 0

    def step_begin(self) -> None:
        """Reset per-step counters. Call at the start of each decode step."""
        self.step_hits = 0
        self.step_total = 0

    def request(
        self,
        tenant_id: str,
        chunk_id: int,
        hmac_tag: int,
    ) -> Tuple[bool, bool]:
        """Process a single promotion request through the CFO CAM.

        Args:
            tenant_id: requesting tenant identifier.
            chunk_id: the KV cache chunk being requested.
            hmac_tag: 64-bit truncated HMAC-SHA256 capability token.

        Returns:
            (dma_needed, coalesced):
              dma_needed=True means a new DMA read must be issued.
              coalesced=True means this request was merged with an existing one.
        """
        self.total_requests += 1
        self.step_total += 1

        # If CAM is gated (low overlap), bypass entirely — every request
        # issues its own DMA (power saving mode, matching RTL cfo_cam_gate).
        if self.cam_gated:
            return True, False

        if chunk_id in self.cam:
            entry = self.cam[chunk_id]
            # HIT path: single-cycle 64-bit equality check
            if entry["hmac_tag"] == hmac_tag:
                # Tag match — coalesce
                entry["tenants"].add(tenant_id)
                entry["access_order"] = self._access_counter
                self._access_counter += 1
                self.step_hits += 1
                self.total_dma_saved += 1
                return False, True
            else:
                # Tag mismatch — different session/capability, issue separate DMA
                return True, False
        else:
            # MISS path: allocate new CAM entry, issue DMA
            if len(self.cam) >= self.cam_entries:
                self._evict_lru()

            self.cam[chunk_id] = {
                "tenants": {tenant_id},
                "hmac_tag": hmac_tag,
                "access_order": self._access_counter,
            }
            self._access_counter += 1
            return True, False

    def step_end(self) -> None:
        """End-of-step processing: update EMA and clock gating state.

        Mirrors RTL logic: EMA alpha=1/32, threshold=0.45 (115/255),
        gate after 8 consecutive low-overlap steps.
        """
        # Compute hit rate sample for this step (Q0.8)
        if self.step_total > 0:
            sample = (self.step_hits * 256) // self.step_total
        else:
            sample = self._ema_hit_rate  # No change if no requests

        # EMA update: ema += (sample - ema) >> 5  (alpha = 1/32)
        self._ema_hit_rate += (sample - self._ema_hit_rate) >> 5

        # Threshold check: 0.45 * 255 ≈ 115
        EMA_THRESHOLD = 115
        GATE_STEPS = 8

        if self._ema_hit_rate < EMA_THRESHOLD:
            self._low_rate_counter += 1
            if self._low_rate_counter >= GATE_STEPS:
                self.cam_gated = True
        else:
            self._low_rate_counter = 0
            self.cam_gated = False

        # Clear stale entries from CAM at step boundary
        # (entries older than 2 steps are unlikely to see further hits)
        self._clear_stale_entries()

    def _evict_lru(self) -> None:
        """Evict the least-recently-used CAM entry."""
        if not self.cam:
            return
        lru_chunk = min(self.cam, key=lambda k: self.cam[k]["access_order"])
        del self.cam[lru_chunk]

    def _clear_stale_entries(self) -> None:
        """Remove entries with no recent access (older than threshold)."""
        if not self.cam:
            return
        # Keep only the most recent cam_entries worth of accesses
        if len(self.cam) > self.cam_entries // 2:
            threshold = self._access_counter - (self.cam_entries * 4)
            stale = [k for k, v in self.cam.items()
                     if v["access_order"] < threshold]
            for k in stale:
                del self.cam[k]

    def get_coalesce_ratio(self) -> float:
        """Return the fraction of requests that were coalesced (DMA saved)."""
        if self.total_requests == 0:
            return 0.0
        return self.total_dma_saved / self.total_requests

    def reset(self) -> None:
        """Reset all state for a fresh simulation run."""
        self.cam.clear()
        self._access_counter = 0
        self.step_hits = 0
        self.step_total = 0
        self._ema_hit_rate = 128
        self._low_rate_counter = 0
        self.cam_gated = False
        self.total_dma_saved = 0
        self.total_requests = 0


# =============================================================================
# SLOAwareScheduler
# =============================================================================

class SLOAwareScheduler:
    """Ensures promotion transfers do not violate per-token latency SLOs.

    For each tenant, the scheduler computes the *SLO slack* -- the gap
    between the SLO target and the current compute-only latency.  If the
    expected promotion latency (exposed, not hidden) would exceed the
    slack, the request is either reduced or deferred entirely.
    """

    def __init__(
        self,
        base_compute_us: float = 120.0,
        prefetch_overlap_fraction: float = 0.85,
    ) -> None:
        self.base_compute_us = base_compute_us
        self.prefetch_overlap_fraction = prefetch_overlap_fraction
        self.slo_violations: Dict[str, int] = {}

    def schedule_with_slo(
        self,
        requests: List[PromotionRequestEntry],
        slo_targets: Dict[str, float],
        available_bandwidth_bytes: Dict[str, int],
        link_bandwidth_gbps: float = 64.0,
    ) -> List[ApprovedPromotion]:
        """Approve or defer promotion requests based on SLO constraints.

        Args:
            requests: per-tenant promotion requests.
            slo_targets: tenant_id -> target per-token latency (us).
            available_bandwidth_bytes: per-tenant bandwidth allocation.
            link_bandwidth_gbps: raw CXL link bandwidth.

        Returns:
            List of approved (possibly reduced or deferred) promotions.
        """
        link_bytes_per_us = link_bandwidth_gbps * 1e9 / 1e6
        approved: List[ApprovedPromotion] = []

        for req in requests:
            tid = req.tenant_id
            slo = slo_targets.get(tid, 500.0)
            alloc = available_bandwidth_bytes.get(tid, 0)

            # Clamp requested bytes to allocation.
            transfer_bytes = min(req.requested_bytes, alloc)
            if transfer_bytes <= 0:
                approved.append(ApprovedPromotion(
                    tenant_id=tid, approved_bytes=0,
                    expected_latency_us=self.base_compute_us, deferred=True,
                ))
                continue

            # Estimate exposed promotion latency after overlap.
            raw_transfer_us = transfer_bytes / link_bytes_per_us
            hidden_us = raw_transfer_us * self.prefetch_overlap_fraction
            exposed_us = max(0.0, raw_transfer_us - hidden_us)

            total_latency_us = self.base_compute_us + exposed_us
            slo_slack = slo - self.base_compute_us

            if exposed_us <= slo_slack:
                # Fits within SLO -- approve in full.
                approved.append(ApprovedPromotion(
                    tenant_id=tid, approved_bytes=transfer_bytes,
                    expected_latency_us=total_latency_us, deferred=False,
                ))
            elif slo_slack > 0:
                # Partially fits -- reduce transfer to stay within SLO.
                max_exposed = slo_slack
                max_raw = max_exposed / max(1.0 - self.prefetch_overlap_fraction, 0.01)
                reduced_bytes = int(max_raw * link_bytes_per_us)
                reduced_bytes = max(0, min(reduced_bytes, transfer_bytes))
                reduced_exposed = (reduced_bytes / link_bytes_per_us) * (
                    1.0 - self.prefetch_overlap_fraction
                )
                approved.append(ApprovedPromotion(
                    tenant_id=tid, approved_bytes=reduced_bytes,
                    expected_latency_us=self.base_compute_us + reduced_exposed,
                    deferred=False,
                ))
                if reduced_bytes < transfer_bytes:
                    self._record_violation(tid)
            else:
                # No slack at all -- defer entirely.
                approved.append(ApprovedPromotion(
                    tenant_id=tid, approved_bytes=0,
                    expected_latency_us=self.base_compute_us, deferred=True,
                ))
                self._record_violation(tid)

        return approved

    def _record_violation(self, tenant_id: str) -> None:
        """Increment the SLO violation counter for a tenant."""
        self.slo_violations[tenant_id] = self.slo_violations.get(tenant_id, 0) + 1

    def get_violations(self) -> Dict[str, int]:
        """Return cumulative SLO violation counts per tenant."""
        return dict(self.slo_violations)


# =============================================================================
# FairnessMetrics
# =============================================================================

class FairnessMetrics:
    """Computes fairness and efficiency metrics across tenants.

    Metrics:
      - Jain's Fairness Index
      - Max / min utility ratio
      - Per-tenant SLO violation rate
      - Bandwidth utilisation efficiency
      - Starvation detection (tenant gets < 10% of fair share for > N steps)
    """

    @staticmethod
    def jains_fairness_index(values: List[float]) -> float:
        """Compute Jain's Fairness Index: (sum x_i)^2 / (n * sum x_i^2).

        Returns 1.0 for perfect fairness, 1/n for maximum unfairness.
        """
        n = len(values)
        if n == 0:
            return 1.0
        s = sum(values)
        ss = sum(v * v for v in values)
        if ss == 0:
            return 1.0
        return (s * s) / (n * ss)

    @staticmethod
    def max_min_ratio(values: List[float]) -> float:
        """Ratio of maximum to minimum value (infinity if min is zero)."""
        if not values:
            return 1.0
        mn = min(values)
        mx = max(values)
        if mn <= 0:
            return float("inf")
        return mx / mn

    @staticmethod
    def slo_violation_rate(
        violations: Dict[str, int],
        total_steps: int,
    ) -> Dict[str, float]:
        """Per-tenant SLO violation rate (violations / total_steps)."""
        if total_steps <= 0:
            return {tid: 0.0 for tid in violations}
        return {tid: count / total_steps for tid, count in violations.items()}

    @staticmethod
    def bandwidth_utilization(
        allocation_history: List[Dict[str, int]],
        total_bandwidth_per_step: int,
    ) -> float:
        """Fraction of total bandwidth actually allocated across all steps."""
        if not allocation_history or total_bandwidth_per_step <= 0:
            return 0.0
        total_allocated = sum(
            sum(alloc.values()) for alloc in allocation_history
        )
        total_available = total_bandwidth_per_step * len(allocation_history)
        return total_allocated / total_available

    @staticmethod
    def detect_starvation(
        allocation_history: List[Dict[str, int]],
        tenant_ids: List[str],
        total_bandwidth_per_step: int,
        threshold_fraction: float = 0.10,
        min_consecutive_steps: int = 5,
    ) -> List[StarvationEvent]:
        """Detect tenants receiving < threshold of fair share for consecutive steps.

        A tenant is considered starved if it receives less than
        ``threshold_fraction`` of its equal-share allocation for at least
        ``min_consecutive_steps`` consecutive steps.
        """
        k = len(tenant_ids)
        if k == 0 or not allocation_history:
            return []

        fair_share = total_bandwidth_per_step / k
        starvation_threshold = fair_share * threshold_fraction

        events: List[StarvationEvent] = []
        streak_start: Dict[str, int] = {}
        streak_len: Dict[str, int] = {tid: 0 for tid in tenant_ids}

        for step, alloc in enumerate(allocation_history):
            for tid in tenant_ids:
                got = alloc.get(tid, 0)
                if got < starvation_threshold:
                    if streak_len[tid] == 0:
                        streak_start[tid] = step
                    streak_len[tid] += 1
                else:
                    if streak_len[tid] >= min_consecutive_steps:
                        events.append(StarvationEvent(
                            tenant_id=tid,
                            start_step=streak_start[tid],
                            end_step=step - 1,
                            duration_steps=streak_len[tid],
                            fair_share_fraction=threshold_fraction,
                        ))
                    streak_len[tid] = 0

        # Close any open streaks at the end of the history.
        for tid in tenant_ids:
            if streak_len[tid] >= min_consecutive_steps:
                events.append(StarvationEvent(
                    tenant_id=tid,
                    start_step=streak_start[tid],
                    end_step=len(allocation_history) - 1,
                    duration_steps=streak_len[tid],
                    fair_share_fraction=threshold_fraction,
                ))

        return events


# =============================================================================
# MultiTenantSimulator
# =============================================================================

class MultiTenantSimulator:
    """End-to-end simulation of K tenants sharing a GPU + CXL memory pool.

    Each tenant runs an independent decode loop with promotion.  Shared
    resources -- HBM capacity, CXL bandwidth, memory controller queue --
    are modelled explicitly so that contention effects are visible.

    Usage::

        sim = MultiTenantSimulator(
            total_hbm_bytes=40 * 1024**3,
            cxl_bandwidth_gbps=64.0,
        )
        result = sim.simulate(tenant_configs, num_steps=100,
                              allocation_policy=AllocationPolicy.MAX_MIN_FAIRNESS)
    """

    def __init__(
        self,
        total_hbm_bytes: int = 40 * 1024**3,
        cxl_bandwidth_gbps: float = 64.0,
        base_compute_us: float = 120.0,
        prefetch_overlap_fraction: float = 0.85,
        controller_queue_depth: int = 16,
    ) -> None:
        self.total_hbm_bytes = total_hbm_bytes
        self.cxl_bandwidth_gbps = cxl_bandwidth_gbps
        self.base_compute_us = base_compute_us

        self.allocator = FairPromotionAllocator()
        self.contention_model = PromotionContentionModel(
            controller_queue_depth=controller_queue_depth,
        )
        self.slo_scheduler = SLOAwareScheduler(
            base_compute_us=base_compute_us,
            prefetch_overlap_fraction=prefetch_overlap_fraction,
        )
        self.cfo_model = CFOCoalesceModel(cam_entries=16)

    # -----------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------

    def simulate(
        self,
        tenant_configs: List[TenantState],
        num_steps: int = 100,
        allocation_policy: AllocationPolicy = AllocationPolicy.EQUAL_SHARE,
        bytes_per_step_per_tenant: int = 64 * 1024,
    ) -> MultiTenantResult:
        """Run the multi-tenant decode simulation.

        Args:
            tenant_configs: initial state for each tenant.
            num_steps: number of decode steps to simulate.
            allocation_policy: bandwidth allocation strategy.
            bytes_per_step_per_tenant: default promotion request size
                per tenant per step (bytes).

        Returns:
            MultiTenantResult with per-tenant and aggregate metrics.
        """
        tenants = list(tenant_configs)
        k = len(tenants)
        if k == 0:
            return MultiTenantResult()

        # Per-step bandwidth budget (bytes).  Convert GB/s to bytes/step
        # assuming each step takes ~base_compute_us.
        step_duration_us = self.base_compute_us
        link_bytes_per_step = int(
            self.cxl_bandwidth_gbps * 1e9 * (step_duration_us / 1e6)
        )

        slo_targets = {t.tenant_id: t.slo_target_latency_us for t in tenants}
        tenant_ids = [t.tenant_id for t in tenants]

        # Accumulators
        per_tenant_latency_sum: Dict[str, float] = {tid: 0.0 for tid in tenant_ids}
        per_tenant_utility_sum: Dict[str, float] = {tid: 0.0 for tid in tenant_ids}
        per_tenant_promoted_bytes: Dict[str, int] = {tid: 0 for tid in tenant_ids}
        contention_events: List[ContentionEvent] = []
        allocation_history: List[Dict[str, int]] = []

        for step in range(num_steps):
            # 1. Each tenant generates a promotion request.
            requests = self._generate_requests(
                tenants, step, bytes_per_step_per_tenant,
            )

            # 1b. CFO coalescing: deduplicate requests for the same chunk
            # across tenants. This reduces effective DMA traffic.
            self.cfo_model.step_begin()
            effective_requests: List[PromotionRequestEntry] = []
            for req in requests:
                # Simulate per-chunk requests within each tenant's batch.
                # Use chunk_count as the number of individual chunk requests.
                coalesced_bytes = 0
                dma_bytes = 0
                chunk_size = max(
                    1, req.requested_bytes // max(1, req.chunk_count)
                )
                for _c in range(req.chunk_count):
                    # Generate a deterministic chunk_id from tenant + step + chunk
                    chunk_id = hash((req.tenant_id, step, _c)) & 0x1FF
                    hmac_tag = hash((req.tenant_id, chunk_id)) & 0xFFFF_FFFF_FFFF_FFFF
                    dma_needed, was_coalesced = self.cfo_model.request(
                        req.tenant_id, chunk_id, hmac_tag,
                    )
                    if dma_needed:
                        dma_bytes += chunk_size
                    else:
                        coalesced_bytes += chunk_size

                # Reduce the effective request size by coalesced amount
                effective_req = PromotionRequestEntry(
                    tenant_id=req.tenant_id,
                    requested_bytes=dma_bytes,
                    expected_utility=req.expected_utility,
                    priority=req.priority,
                    chunk_count=max(1, dma_bytes // max(1, chunk_size)),
                )
                effective_requests.append(effective_req)
            self.cfo_model.step_end()

            # 2. Allocate bandwidth (using effective post-coalesce requests).
            alloc = self.allocator.allocate(
                tenants, link_bytes_per_step, allocation_policy,
            )
            allocation_history.append(alloc)

            # 3. Model contention (using effective post-coalesce requests).
            eff_bw, cont_latency = self.contention_model.simulate_contention(
                effective_requests, self.cxl_bandwidth_gbps,
            )

            # Record contention if total demand exceeds supply.
            total_requested = sum(r.requested_bytes for r in effective_requests)
            if total_requested > link_bytes_per_step:
                max_lat = max(cont_latency.values()) if cont_latency else 0.0
                contention_events.append(ContentionEvent(
                    step=step,
                    tenant_ids=list(cont_latency.keys()),
                    total_requested_bytes=total_requested,
                    available_bandwidth_bytes=link_bytes_per_step,
                    queuing_delay_us=max(0.0, max_lat - self.base_compute_us),
                    hol_blocking_us=0.0,
                ))

            # 4. SLO-aware scheduling.
            approved = self.slo_scheduler.schedule_with_slo(
                requests, slo_targets, alloc, self.cxl_bandwidth_gbps,
            )

            # 5. Apply promotions and accumulate metrics.
            for ap in approved:
                tid = ap.tenant_id
                per_tenant_latency_sum[tid] += ap.expected_latency_us
                promoted = ap.approved_bytes
                per_tenant_promoted_bytes[tid] += promoted

                # Utility model: diminishing returns on bytes promoted.
                utility = math.log1p(promoted / 1024.0) if promoted > 0 else 0.0
                per_tenant_utility_sum[tid] += utility

                # Update tenant state.
                tenant = next(t for t in tenants if t.tenant_id == tid)
                tenant.current_hbm_usage += promoted
                tenant.current_tail_size = max(0, tenant.current_tail_size - promoted)
                tenant.promotion_history.append(
                    (float(step), promoted, utility),
                )

        # -----------------------------------------------------------------
        # Aggregate results
        # -----------------------------------------------------------------
        per_tenant_throughput: Dict[str, float] = {}
        per_tenant_latency: Dict[str, float] = {}
        per_tenant_utility: Dict[str, float] = {}

        for tid in tenant_ids:
            per_tenant_latency[tid] = per_tenant_latency_sum[tid] / max(num_steps, 1)
            per_tenant_utility[tid] = per_tenant_utility_sum[tid]
            avg_lat_us = per_tenant_latency[tid]
            per_tenant_throughput[tid] = 1e6 / avg_lat_us if avg_lat_us > 0 else 0.0

        utilities = list(per_tenant_utility.values())
        fairness_index = FairnessMetrics.jains_fairness_index(utilities)
        bw_util = FairnessMetrics.bandwidth_utilization(
            allocation_history, link_bytes_per_step,
        )
        starvation = FairnessMetrics.detect_starvation(
            allocation_history, tenant_ids, link_bytes_per_step,
        )

        return MultiTenantResult(
            per_tenant_throughput=per_tenant_throughput,
            per_tenant_latency=per_tenant_latency,
            per_tenant_utility=per_tenant_utility,
            fairness_index=fairness_index,
            bandwidth_utilization=bw_util,
            slo_violations_per_tenant=self.slo_scheduler.get_violations(),
            contention_events=contention_events,
            starvation_events=starvation,
            allocation_history=allocation_history,
        )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _generate_requests(
        tenants: List[TenantState],
        step: int,
        default_bytes: int,
    ) -> List[PromotionRequestEntry]:
        """Generate per-tenant promotion requests for a single step.

        Each tenant requests up to *default_bytes* or its remaining tail
        size, whichever is smaller.  Expected utility is estimated from
        recent promotion history.

        Priority is quantized to 4-bit (0..15) to match the RTL VC-WRR
        hardware granularity (WEIGHT_W=4).
        """
        requests: List[PromotionRequestEntry] = []
        # Find max priority for normalization
        max_prio = max((t.priority for t in tenants), default=1.0)
        for t in tenants:
            req_bytes = min(default_bytes, t.current_tail_size)
            if req_bytes <= 0:
                req_bytes = 0
            # Quantize priority to 4-bit hardware weight
            hw_weight = quantize_priority_to_4bit(t.priority, max_prio)
            quantized_priority = hw_weight / VC_WRR_MAX_WEIGHT
            requests.append(PromotionRequestEntry(
                tenant_id=t.tenant_id,
                requested_bytes=req_bytes,
                expected_utility=t.recent_utility(),
                priority=quantized_priority,
                chunk_count=max(1, req_bytes // (64 * 1024)),
            ))
        return requests
