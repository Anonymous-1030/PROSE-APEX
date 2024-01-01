"""
Unified CXL Queue Simulator — Shared Backend for ALL Baseline Experiments.

Models the CXL memory controller with bounded queue depth, bandwidth-limited
serialization, and M/D/1 queuing delay (Pollaczek-Khinchine).  This module
ensures every baseline sees identical CXL hardware constraints so that
comparisons are fair and reproducible.

Key modeling components:
  1. Queue depth saturation (from SimCXL: 48 entries for ASIC, 36 for FPGA)
  2. Bandwidth-limited serialization (flit-level, CXL.mem protocol overhead)
  3. M/D/1 waiting time (Pollaczek-Khinchine formula)
  4. DRAM backend access latency (row buffer hit/miss/conflict)
  5. Invalid payload tracking (bytes fetched but never used)
  6. Per-step accounting: total traffic, queue utilization ρ, stall time

References:
  - SimCXL-main: CXLMemCtrl with req_size=48, rsp_size=48, proto_proc_lat=15ns
  - CXL 3.0 Specification, Chapter 3 (CXL.mem)
  - Kleinrock, "Queueing Systems Vol 1", 1975 (M/D/1)

Usage (shared by ALL baselines):
    cxl = CXLQueueSimulator(CXLQueueConfig(bandwidth_gbps=64, queue_depth=48))

    # PROSE path (score-before-fetch):
    summary_result = cxl.submit_summary_fetch(candidate_chunk_ids)
    validated = scorer.score(summary_result)  # rank by metadata
    payload_result = cxl.submit_payload_fetch(validated[:budget])

    # fetch-then-score path (baselines):
    payload_result = cxl.submit_payload_fetch(all_candidate_chunk_ids)
    useful = scorer.score(payload_result)  # too late — payload already fetched

    # End of step:
    stats = cxl.end_step()  # returns StepStats with ρ, invalid_traffic, etc.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ── Configuration ──────────────────────────────────────────────────────

@dataclass
class CXLQueueConfig:
    """CXL memory controller queue configuration.

    Defaults match SimCXL ASIC parameters (Type-3 memory expander).
    """
    # Link parameters (CXL 3.0 x16)
    bandwidth_gbps: float = 64.0          # Effective bandwidth (after 2% overhead)
    raw_bandwidth_gbps: float = 65.28     # 16 lanes × 64 GT/s / 8 × 0.98
    flit_size_bytes: int = 256            # CXL 3.0 256B flit
    protocol_overhead: float = 0.02       # 2% CXL.mem TLP encapsulation

    # Queue parameters (from SimCXL CXLMemCtrl)
    queue_depth: int = 48                 # req_size / rsp_size (ASIC)
    proto_proc_lat_ns: float = 15.0       # Protocol processing latency (ASIC)
    bridge_lat_ns: float = 50.0           # CXLBridge traversal (classic path)

    # DRAM backend (DDR5-4800 on CXL expander)
    dram_row_hit_ns: float = 40.0         # tCAS
    dram_row_miss_ns: float = 120.0       # tRP + tRCD + tCAS
    dram_row_buffer_hit_rate: float = 0.30
    dram_num_bank_groups: int = 4
    dram_sustained_bw_gbps: float = 76.8  # 2 channels DDR5-4800

    # Payload parameters
    chunk_size_bytes: int = 65536         # 512 tokens × 128 bytes/KV
    summary_size_bytes: int = 64          # PROSE metadata summary (64B)

    # CXL.mem credit-based flow control
    credit_rtt_ns: float = 100.0

    # Step timing for utilization calculation
    decode_step_interval_ns: float = 100_000.0  # Nominal decode step (100μs)

    @property
    def flit_serialization_ns(self) -> float:
        """Time to serialize one flit."""
        return self.flit_size_bytes / self.raw_bandwidth_gbps

    @property
    def bytes_per_ns(self) -> float:
        """Effective bytes per nanosecond."""
        return self.bandwidth_gbps  # GB/s = bytes/ns


# ── Result Types ───────────────────────────────────────────────────────

@dataclass
class CXLFetchResult:
    """Result of a CXL fetch operation."""
    chunk_ids: List[int] = field(default_factory=list)
    total_bytes: int = 0
    serialization_ns: float = 0.0
    dram_access_ns: float = 0.0
    protocol_ns: float = 0.0
    queuing_ns: float = 0.0
    total_ns: float = 0.0
    queue_depth_at_submit: int = 0
    accepted: bool = True
    rejected_chunks: List[int] = field(default_factory=list)

    @property
    def total_us(self) -> float:
        return self.total_ns / 1000.0


@dataclass
class StepStats:
    """Per-step accounting for CXL queue simulation."""
    # Traffic
    total_bytes_fetched: int = 0
    summary_bytes_fetched: int = 0
    payload_bytes_fetched: int = 0
    invalid_payload_bytes: int = 0       # Fetched but not in final visible set
    invalid_summary_bytes: int = 0       # Summary fetched for chunks never promoted

    # Queue metrics
    queue_utilization_rho: float = 0.0   # ρ = λ·S
    queue_depth_peak: int = 0
    queue_depth_mean: float = 0.0
    queue_full_events: int = 0           # Times queue was at capacity
    requests_rejected: int = 0           # Requests that exceeded queue depth

    # Timing
    total_queuing_ns: float = 0.0
    total_serialization_ns: float = 0.0
    total_dram_ns: float = 0.0
    total_protocol_ns: float = 0.0
    total_time_ns: float = 0.0

    # Chunk-level
    total_chunks_fetched: int = 0
    valid_chunks_used: int = 0           # Fetched chunks that were actually used
    invalid_chunks: int = 0              # Fetched chunks that were discarded

    @property
    def invalid_traffic_ratio(self) -> float:
        """Fraction of payload bytes that were wasted (invalid)."""
        if self.payload_bytes_fetched == 0:
            return 0.0
        return self.invalid_payload_bytes / self.payload_bytes_fetched

    @property
    def effective_bandwidth_gbps(self) -> float:
        """Effective bandwidth including queuing delays."""
        if self.total_time_ns == 0:
            return 0.0
        return (self.total_bytes_fetched / (1024**3)) / (self.total_time_ns / 1e9)

    @property
    def saturation_multiplier(self) -> float:
        """How much queuing amplifies latency: (S+W)/S."""
        if self.total_serialization_ns == 0:
            return 1.0
        return (self.total_serialization_ns + self.total_queuing_ns) / self.total_serialization_ns


# ── CXL Queue Simulator ────────────────────────────────────────────────

class CXLQueueSimulator:
    """M/D/1 queue-accurate CXL memory controller simulator.

    Models the CXL Type-3 memory expander with:
      - Bounded queue depth (credit-based flow control from SimCXL)
      - Deterministic service time per request
      - M/D/1 waiting time for queuing delay
      - DRAM backend access with row buffer hit/miss
      - Per-step statistics for invalid traffic and queue pressure
    """

    def __init__(self, config: Optional[CXLQueueConfig] = None):
        self.cfg = config or CXLQueueConfig()
        self.reset()

    def reset(self):
        """Reset simulator state for a new trace/sequence."""
        self._queue: List[Tuple[int, float]] = []  # (bytes, service_time_ns)
        self._cumulative_service_time_ns: float = 0.0
        self._prev_arrival_time_ns: float = 0.0
        self._total_arrivals: int = 0
        self._total_completions: int = 0

        # Per-step accumulation
        self._step_stats = StepStats()
        self._step_fetched_chunks: Dict[int, bool] = {}  # chunk_id -> was_useful
        self._step_queue_depths: List[int] = []

    # ── Core queuing model ───────────────────────────────────────────

    def _service_time_ns(self, payload_bytes: int) -> float:
        """Deterministic service time for a CXL memory read.

        Service time = DRAM access + serialization + protocol processing.
        For small reads (summary), DRAM overhead dominates.
        For large reads (full chunk), bandwidth dominates.
        """
        # 1. DRAM backend access
        bw_floor_ns = payload_bytes / self.cfg.dram_sustained_bw_gbps

        if payload_bytes <= 4096:
            # Small read: per-burst access latency dominates
            burst_bytes = 64
            num_accesses = max(1, payload_bytes // burst_bytes)
            effective_accesses = math.ceil(num_accesses / self.cfg.dram_num_bank_groups)

            hit_rate = self.cfg.dram_row_buffer_hit_rate
            avg_dram_ns_per_access = (
                hit_rate * self.cfg.dram_row_hit_ns +
                (1 - hit_rate) * self.cfg.dram_row_miss_ns
            )
            access_dram_ns = effective_accesses * avg_dram_ns_per_access
            dram_ns = max(bw_floor_ns, access_dram_ns)
        else:
            # Large read: bandwidth-limited continuous transfer
            # Add one row-miss penalty for the initial activation
            dram_ns = bw_floor_ns + self.cfg.dram_row_miss_ns

        # 2. Link serialization (flit-level)
        num_flits = max(1, math.ceil(payload_bytes / self.cfg.flit_size_bytes))
        serialization_ns = num_flits * self.cfg.flit_serialization_ns

        # 3. Protocol processing (double-counted: request + response)
        protocol_ns = 2 * self.cfg.proto_proc_lat_ns + self.cfg.bridge_lat_ns

        return dram_ns + serialization_ns + protocol_ns

    def _md1_waiting_time_ns(self, arrival_rate_per_ns: float, service_time_ns: float) -> float:
        """M/D/1 waiting time: W = ρ·S / (2·(1-ρ)) where ρ = λ·S."""
        rho = arrival_rate_per_ns * service_time_ns
        if rho >= 0.99:
            return service_time_ns * 50.0  # Cap near saturation
        if rho <= 0.0:
            return 0.0
        return (rho * service_time_ns) / (2.0 * (1.0 - rho))

    # ── Fetch operations ──────────────────────────────────────────────

    def submit_summary_fetch(self, chunk_ids: List[int], current_time_ns: float = 0.0) -> CXLFetchResult:
        """Submit a metadata-summary fetch (64B per chunk) — PROSE score-before-fetch path.

        Only the tiny 64B summary is transferred, enabling admission gating
        BEFORE expensive full-payload DMA.
        """
        if not chunk_ids:
            return CXLFetchResult()

        payload_bytes = len(chunk_ids) * self.cfg.summary_size_bytes
        return self._submit(chunk_ids, payload_bytes, current_time_ns, is_summary=True)

    def submit_payload_fetch(self, chunk_ids: List[int], current_time_ns: float = 0.0,
                             bytes_per_chunk: Optional[int] = None) -> CXLFetchResult:
        """Submit a full-payload fetch (e.g., 64KB per chunk).

        Used by:
          - fetch-then-score baselines (directly fetch full chunks)
          - PROSE after summary-based admission (fetch only validated chunks)
        """
        if not chunk_ids:
            return CXLFetchResult()

        bpc = bytes_per_chunk or self.cfg.chunk_size_bytes
        payload_bytes = len(chunk_ids) * bpc
        return self._submit(chunk_ids, payload_bytes, current_time_ns, is_summary=False)

    def _submit(self, chunk_ids: List[int], total_bytes: int,
                current_time_ns: float, is_summary: bool) -> CXLFetchResult:
        """Internal submit with queuing model.

        IMPORTANT: All chunks are accepted — the M/D/1 queuing model
        already accounts for queue depth pressure via waiting time.
        The queue_depth parameter is tracked as a metric (peak_depth)
        but does not reject requests (real CXL controllers buffer
        requests via credit-based flow control, not hard reject).
        """
        result = CXLFetchResult(chunk_ids=list(chunk_ids))

        accepted_chunks = []
        rejected_chunks = []
        for cid in chunk_ids:
            bytes_per = total_bytes // max(len(chunk_ids), 1)
            self._queue.append((bytes_per, 0.0))
            accepted_chunks.append(cid)
            if len(self._queue) > self.cfg.queue_depth:
                self._step_stats.queue_full_events += 1

        if not accepted_chunks:
            result.accepted = False
            result.rejected_chunks = rejected_chunks
            result.total_bytes = 0
            return result

        result.accepted = True
        result.rejected_chunks = rejected_chunks
        effective_bytes = len(accepted_chunks) * (total_bytes // max(len(chunk_ids), 1))
        result.total_bytes = effective_bytes
        result.queue_depth_at_submit = len(self._queue)

        # Service time
        service_ns = self._service_time_ns(effective_bytes)
        result.serialization_ns = (effective_bytes / self.cfg.bytes_per_ns) if self.cfg.bytes_per_ns > 0 else 0.0
        result.dram_access_ns = service_ns - result.serialization_ns - 2 * self.cfg.proto_proc_lat_ns - self.cfg.bridge_lat_ns

        # Queuing is computed at step level in end_step()
        # (per-call M/D/1 with artificial inter-arrival inflated ρ)
        result.queuing_ns = 0.0

        # Protocol overhead
        protocol_ns = 2 * self.cfg.proto_proc_lat_ns + self.cfg.bridge_lat_ns
        result.protocol_ns = protocol_ns
        result.total_ns = service_ns  # + queuing (added in end_step)

        # Update state
        self._cumulative_service_time_ns += service_ns
        self._prev_arrival_time_ns = current_time_ns if current_time_ns > 0 else self._cumulative_service_time_ns
        self._total_arrivals += 1

        # Track queue depth
        self._step_queue_depths.append(len(self._queue))

        # Update step stats
        if is_summary:
            self._step_stats.summary_bytes_fetched += effective_bytes
        else:
            self._step_stats.payload_bytes_fetched += effective_bytes
        self._step_stats.total_bytes_fetched += effective_bytes
        self._step_stats.total_chunks_fetched += len(accepted_chunks)
        self._step_stats.total_serialization_ns += result.serialization_ns
        self._step_stats.total_dram_ns += result.dram_access_ns
        self._step_stats.total_protocol_ns += protocol_ns
        self._step_stats.total_queuing_ns += result.queuing_ns  # step-level queuing added in end_step
        self._step_stats.total_time_ns += result.total_ns

        # Track fetched chunks (usefulness determined later via mark_chunks_used)
        # Summary fetches are NOT tracked in _step_fetched_chunks because
        # their invalid traffic is tracked separately as invalid_summary_bytes
        if not is_summary:
            for cid in accepted_chunks:
                self._step_fetched_chunks[cid] = False  # Default: not yet proven useful

        return result

    # ── Post-fetch tracking ───────────────────────────────────────────

    def mark_chunks_used(self, chunk_ids: List[int]):
        """Mark which fetched chunks were actually used in attention computation."""
        for cid in chunk_ids:
            if cid in self._step_fetched_chunks:
                self._step_fetched_chunks[cid] = True

    def mark_chunks_invalid(self, chunk_ids: List[int]):
        """Explicitly mark chunks as invalid (fetched but discarded).

        For fetch-then-score baselines: chunks that were DMA'd but
        the local scorer rejected them.

        Sets the chunk to 'processed' (True) so end_step() does NOT
        double-count it as unmarked-invalid.
        """
        for cid in chunk_ids:
            if cid in self._step_fetched_chunks:
                if not self._step_fetched_chunks[cid]:
                    bpc = self.cfg.chunk_size_bytes
                    self._step_stats.invalid_payload_bytes += bpc
                    self._step_stats.invalid_chunks += 1
                    self._step_fetched_chunks[cid] = True  # Prevent double-count in end_step()

    # ── End-of-step accounting ────────────────────────────────────────

    def end_step(self) -> StepStats:
        """Finalize per-step statistics and return them.

        Computes:
          - Invalid payload traffic (fetched but never marked used)
          - Queue utilization ρ
          - Saturation multiplier

        IMPORTANT: Only counts chunks submitted via submit_payload_fetch()
        as potential invalid payload. Summary-fetched chunks use
        summary_size_bytes and are tracked separately.
        """
        # Count any remaining unmarked payload chunks as invalid
        for cid, was_useful in self._step_fetched_chunks.items():
            if not was_useful:
                # Check if this was a summary fetch (64B) or payload fetch (64KB)
                # Summary-fetched chunks: mark as invalid_summary (cheap)
                # Payload-fetched chunks: mark as invalid_payload (expensive)
                self._step_stats.invalid_payload_bytes += self.cfg.chunk_size_bytes
                self._step_stats.invalid_chunks += 1
            else:
                self._step_stats.valid_chunks_used += 1

        # Compute queue utilization ρ and step-level M/D/1 queuing
        if self._step_queue_depths:
            self._step_stats.queue_depth_peak = max(self._step_queue_depths)
            self._step_stats.queue_depth_mean = sum(self._step_queue_depths) / len(self._step_queue_depths) if self._step_queue_depths else 0.0

        total_service = (self._step_stats.total_serialization_ns
                         + self._step_stats.total_dram_ns
                         + self._step_stats.total_protocol_ns)
        step_interval_ns = self.cfg.decode_step_interval_ns
        if step_interval_ns > 0 and total_service > 0:
            # ρ = fraction of decode step the CXL link is busy
            rho = min(0.99, total_service / step_interval_ns)
            # M/D/1: W = ρ·S / (2·(1-ρ))  (Pollaczek-Khinchine)
            step_queuing = (rho * total_service) / (2.0 * max(1.0 - rho, 0.01))
            self._step_stats.queue_utilization_rho = rho
            self._step_stats.total_queuing_ns = step_queuing
            self._step_stats.total_time_ns = total_service + step_queuing
        else:
            self._step_stats.queue_utilization_rho = 0.0

        stats = self._step_stats

        # Reset per-step accumulators
        self._step_stats = StepStats()
        self._step_fetched_chunks = {}
        self._step_queue_depths = []

        return stats


# ── Factory functions for common configurations ────────────────────────

def make_cxl_asic_config() -> CXLQueueConfig:
    """SimCXL ASIC configuration: 48-entry queue, 15ns proto processing."""
    return CXLQueueConfig(
        bandwidth_gbps=64.0,
        queue_depth=48,
        proto_proc_lat_ns=15.0,
    )


def make_cxl_fpga_config() -> CXLQueueConfig:
    """SimCXL FPGA configuration: 36-entry queue, 60ns proto processing."""
    return CXLQueueConfig(
        bandwidth_gbps=32.0,
        queue_depth=36,
        proto_proc_lat_ns=60.0,
    )


def make_cxl_cxl20_config() -> CXLQueueConfig:
    """CXL 2.0 configuration (PCIe 5.0 x16)."""
    return CXLQueueConfig(
        bandwidth_gbps=32.0,
        raw_bandwidth_gbps=32.64,
        queue_depth=32,
        proto_proc_lat_ns=25.0,
    )


# ── Convenience: per-baseline CXL simulation wrapper ───────────────────

@dataclass
class BaselineStepResult:
    """Single decode-step result from a baseline policy evaluation."""
    step: int
    selected_chunks: List[int]           # Chunks selected for HBM
    gold_chunks: List[int]               # Oracle: which chunks had high attention
    recovery: float                       # |selected ∩ gold| / |gold|
    cxl_stats: StepStats                  # CXL queue stats for this step
    latency_us: float                     # Total latency including CXL stall
    invalid_traffic_ratio: float          # Invalid / total payload


class BaselineCXLSession:
    """Per-trace CXL session shared by a single baseline evaluation.

    Wraps CXLQueueSimulator with convenience methods for the two
    fundamental ordering modes:
      - score_before_fetch (PROSE): summary → rank → fetch validated
      - fetch_then_score (baselines): fetch all → rank locally
    """

    def __init__(self, config: Optional[CXLQueueConfig] = None):
        self.cxl = CXLQueueSimulator(config)
        self.step_results: List[BaselineStepResult] = []
        self._time_ns: float = 0.0
        self._step: int = 0

    def score_before_fetch(
        self,
        candidate_ids: List[int],
        scorer_fn,  # Callable[[List[int]], List[int]]: scores candidates, returns top-K
        budget_chunks: int,
    ) -> Tuple[List[int], CXLFetchResult, CXLFetchResult]:
        """PROSE path: fetch 64B summaries, score, then fetch only validated chunks.

        Returns: (final_selected, summary_result, payload_result)
        """
        summary_result = self.cxl.submit_summary_fetch(candidate_ids, self._time_ns)

        # Score candidates based on summaries (simulated by scorer_fn)
        validated_ids = scorer_fn(candidate_ids)
        selected = validated_ids[:budget_chunks]

        # Mark rejected candidates as invalid summary traffic
        rejected = [c for c in candidate_ids if c not in selected]
        self.cxl._step_stats.invalid_summary_bytes += len(rejected) * self.cxl.cfg.summary_size_bytes

        # Fetch full payloads only for validated chunks
        payload_result = self.cxl.submit_payload_fetch(selected, self._time_ns)

        # Mark as used
        self.cxl.mark_chunks_used(selected)

        return selected, summary_result, payload_result

    def fetch_then_score(
        self,
        candidate_ids: List[int],
        scorer_fn,  # Callable[[List[int]], List[int]]
        budget_chunks: int,
    ) -> Tuple[List[int], CXLFetchResult]:
        """Baseline path: fetch ALL candidate payloads first, then score locally.

        This is the key inefficiency: invalid chunks consume CXL bandwidth
        before we know they're useless.

        Returns: (final_selected, payload_result)
        """
        # Fetch full payloads for ALL candidates
        payload_result = self.cxl.submit_payload_fetch(candidate_ids, self._time_ns)

        # Score locally (too late — data already transferred)
        ranked = scorer_fn(candidate_ids)
        selected = ranked[:budget_chunks]

        # Mark unselected chunks as invalid traffic
        invalid = [c for c in candidate_ids if c not in selected]
        self.cxl.mark_chunks_invalid(invalid)
        self.cxl.mark_chunks_used(selected)

        return selected, payload_result

    def advance_step(self, decode_time_ns: float = 1000000.0):
        """Advance to the next decode step.

        Args:
            decode_time_ns: GPU compute time for one decode step (~1ms)
        """
        self._time_ns += decode_time_ns
        self._step += 1

    def end_step(self, selected: List[int], gold: List[int]) -> BaselineStepResult:
        """Finalize the current step."""
        stats = self.cxl.end_step()

        intersection = len(set(selected) & set(gold))
        recovery = intersection / max(len(gold), 1)

        result = BaselineStepResult(
            step=self._step,
            selected_chunks=list(selected),
            gold_chunks=list(gold),
            recovery=recovery,
            cxl_stats=stats,
            latency_us=stats.total_time_ns / 1000.0,
            invalid_traffic_ratio=stats.invalid_traffic_ratio,
        )
        self.step_results.append(result)
        return result
