"""Descriptor Batching and Virtual Channel Arbitration for Multi-Host CXL.

Addresses Reviewer Concern #3: Under 8-host concurrent stress test, average
admission latency inflates 21× to 990 ns. Frequent per-descriptor MMIO
triggers cause endpoint queue saturation and backpressure.

Solution: Two-pronged approach:

  1. Descriptor Coalescing (Host-side):
     Instead of per-descriptor MMIO commands, the host driver packs all K
     chunk descriptors for the current step into a single Batch Descriptor
     Block in HBM, then triggers ONE MMIO Doorbell. The endpoint DMA-pulls
     the entire batch in a single burst.

  2. Virtual Channel (VC) Arbitration (Endpoint-side):
     Each tenant/host gets a dedicated hardware queue (Virtual Channel).
     A Weighted Round-Robin (WRR) arbiter services VCs fairly, preventing
     any single tenant from monopolizing the endpoint pipeline.

Combined effect: eliminates per-descriptor MMIO overhead and provides
hardware-level isolation between tenants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import deque

import numpy as np


@dataclass
class DescriptorBatchConfig:
    """Configuration for host-side descriptor batching."""

    max_batch_size: int = 64          # max descriptors per batch
    descriptor_bytes: int = 64        # bytes per descriptor
    doorbell_latency_ns: float = 30.0  # single MMIO doorbell cost (optimized)
    dma_pull_latency_ns: float = 50.0  # endpoint DMA pull for batch (CXL 3.0)
    hbm_bandwidth_gbps: float = 3350.0  # HBM bandwidth for batch assembly

    @property
    def max_batch_bytes(self) -> int:
        return self.max_batch_size * self.descriptor_bytes

    def batch_assembly_ns(self, n_descriptors: int) -> float:
        """Time to assemble batch in HBM (negligible vs doorbell)."""
        batch_bytes = n_descriptors * self.descriptor_bytes
        return batch_bytes / (self.hbm_bandwidth_gbps * 1e9 / 1e9)  # ns

    def total_batch_latency_ns(self, n_descriptors: int) -> float:
        """Total latency for one batched submission."""
        assembly = self.batch_assembly_ns(n_descriptors)
        return assembly + self.doorbell_latency_ns + self.dma_pull_latency_ns


@dataclass
class VirtualChannelConfig:
    """Configuration for per-tenant virtual channels."""

    num_channels: int = 8             # max tenants supported
    per_channel_depth: int = 32       # descriptors per VC queue
    arbiter_policy: str = "wrr"       # "wrr" or "strict_priority"
    wrr_quantum: int = 8              # descriptors per WRR round
    channel_isolation: bool = True    # prevent cross-VC interference
    starvation_guard_rounds: int = 4  # force service after N skips


@dataclass
class MultiHostEndpointConfig:
    """Combined configuration for the multi-host optimized endpoint."""

    batch: DescriptorBatchConfig = field(default_factory=DescriptorBatchConfig)
    vc: VirtualChannelConfig = field(default_factory=VirtualChannelConfig)

    # Endpoint pipeline (same as CEFE-Lite but with VC front-end)
    pipeline_stages: int = 3
    clock_mhz: float = 800.0  # optimized for CXL 3.0 controller

    # Backpressure
    backpressure_threshold: float = 0.85  # VC fill level to assert BP

    @property
    def cycle_ns(self) -> float:
        return 1000.0 / self.clock_mhz


@dataclass
class BatchDescriptorBlock:
    """A batch of descriptors submitted by one host in a single doorbell."""

    host_id: int
    descriptors: List[Dict]  # each: {chunk_id, priority, bytes, ...}
    submission_time_ns: float = 0.0
    batch_id: int = 0


@dataclass
class VCState:
    """Runtime state of a single Virtual Channel."""

    channel_id: int
    host_id: int
    queue: deque = field(default_factory=lambda: deque(maxlen=32))
    total_enqueued: int = 0
    total_serviced: int = 0
    total_dropped: int = 0
    rounds_since_service: int = 0
    weight: float = 1.0

    @property
    def fill_level(self) -> float:
        if self.queue.maxlen is None or self.queue.maxlen == 0:
            return 0.0
        return len(self.queue) / self.queue.maxlen

    @property
    def is_empty(self) -> bool:
        return len(self.queue) == 0


@dataclass
class ArbiterDecision:
    """One arbitration decision: which VC to service next."""

    channel_id: int
    host_id: int
    descriptor: Dict
    wait_time_ns: float


@dataclass
class MultiHostLatencyResult:
    """Latency results for multi-host simulation."""

    per_host_mean_ns: Dict[int, float]
    per_host_p99_ns: Dict[int, float]
    per_host_max_ns: Dict[int, float]
    aggregate_mean_ns: float
    aggregate_p99_ns: float
    aggregate_max_ns: float
    fairness_index: float
    total_descriptors_processed: int
    total_dropped: int
    backpressure_events: int
    throughput_descriptors_per_us: float


class DescriptorBatcher:
    """Host-side descriptor batching engine.

    Instead of issuing one MMIO per descriptor (original design), the host
    driver assembles all K descriptors for the current decode step into a
    contiguous Batch Descriptor Block in HBM, then triggers a single MMIO
    Doorbell to notify the endpoint.

    Latency comparison (K=25 descriptors):
      Original: 25 × MMIO_latency = 25 × 200ns = 5000 ns
      Batched:  1 × (assembly + doorbell + DMA_pull) = ~130 ns

    Reduction: ~38× for K=25.
    """

    def __init__(self, config: DescriptorBatchConfig):
        self.config = config
        self._batch_counter = 0

    def create_batch(
        self,
        host_id: int,
        descriptors: List[Dict],
        current_time_ns: float = 0.0,
    ) -> Tuple[BatchDescriptorBlock, float]:
        """Create a batch from individual descriptors.

        Returns (batch, latency_ns) where latency is the time to
        assemble and submit the batch.
        """
        n = min(len(descriptors), self.config.max_batch_size)
        batch_descs = descriptors[:n]

        self._batch_counter += 1
        batch = BatchDescriptorBlock(
            host_id=host_id,
            descriptors=batch_descs,
            submission_time_ns=current_time_ns,
            batch_id=self._batch_counter,
        )

        latency = self.config.total_batch_latency_ns(n)
        return batch, latency

    def compare_vs_individual(self, n_descriptors: int) -> Dict[str, float]:
        """Compare batched vs individual MMIO submission latency."""
        individual_mmio_ns = 200.0  # typical MMIO round-trip
        individual_total = n_descriptors * individual_mmio_ns
        batched_total = self.config.total_batch_latency_ns(n_descriptors)

        return {
            "individual_total_ns": individual_total,
            "batched_total_ns": batched_total,
            "speedup": individual_total / max(batched_total, 1.0),
            "n_descriptors": n_descriptors,
        }


class WRRArbiter:
    """Weighted Round-Robin arbiter for Virtual Channel scheduling.

    Services each VC in proportion to its weight, with a starvation
    guard that forces service after N consecutive skips.

    Hardware implementation: simple counter array + priority encoder.
    Area: ~0.001 mm² at 7nm (negligible).
    """

    def __init__(self, config: VirtualChannelConfig):
        self.config = config
        self._channels: Dict[int, VCState] = {}
        self._current_vc_idx: int = 0
        self._quantum_remaining: Dict[int, int] = {}
        self._total_decisions: int = 0

    def register_channel(self, channel_id: int, host_id: int, weight: float = 1.0) -> None:
        """Register a new Virtual Channel for a host."""
        vc = VCState(
            channel_id=channel_id,
            host_id=host_id,
            queue=deque(maxlen=self.config.per_channel_depth),
            weight=weight,
        )
        self._channels[channel_id] = vc
        self._quantum_remaining[channel_id] = int(self.config.wrr_quantum * weight)

    def enqueue(self, channel_id: int, descriptor: Dict) -> bool:
        """Enqueue a descriptor into a VC. Returns False if dropped."""
        vc = self._channels.get(channel_id)
        if vc is None:
            return False

        if len(vc.queue) >= self.config.per_channel_depth:
            vc.total_dropped += 1
            return False

        vc.queue.append(descriptor)
        vc.total_enqueued += 1
        return True

    def enqueue_batch(self, channel_id: int, batch: BatchDescriptorBlock) -> int:
        """Enqueue an entire batch into a VC. Returns number accepted."""
        accepted = 0
        for desc in batch.descriptors:
            if self.enqueue(channel_id, desc):
                accepted += 1
        return accepted

    def arbitrate(self) -> Optional[ArbiterDecision]:
        """Select next descriptor to service using WRR policy."""
        if not self._channels:
            return None

        active_ids = sorted(self._channels.keys())
        n_active = len(active_ids)
        if n_active == 0:
            return None

        # Try each VC in round-robin order
        for _ in range(n_active):
            vc_id = active_ids[self._current_vc_idx % n_active]
            vc = self._channels[vc_id]

            if not vc.is_empty and self._quantum_remaining.get(vc_id, 0) > 0:
                desc = vc.queue.popleft()
                vc.total_serviced += 1
                vc.rounds_since_service = 0
                self._quantum_remaining[vc_id] -= 1
                self._total_decisions += 1

                # Reset other VCs' starvation counters
                for other_id, other_vc in self._channels.items():
                    if other_id != vc_id and not other_vc.is_empty:
                        other_vc.rounds_since_service += 1

                return ArbiterDecision(
                    channel_id=vc_id,
                    host_id=vc.host_id,
                    descriptor=desc,
                    wait_time_ns=0.0,
                )

            # Quantum exhausted or empty — move to next
            self._current_vc_idx += 1

            # Starvation guard
            if vc.rounds_since_service >= self.config.starvation_guard_rounds and not vc.is_empty:
                desc = vc.queue.popleft()
                vc.total_serviced += 1
                vc.rounds_since_service = 0
                self._total_decisions += 1
                return ArbiterDecision(
                    channel_id=vc_id,
                    host_id=vc.host_id,
                    descriptor=desc,
                    wait_time_ns=0.0,
                )

        # Refill quantums if all exhausted
        all_exhausted = all(
            self._quantum_remaining.get(vid, 0) <= 0
            for vid in active_ids
        )
        if all_exhausted:
            for vid in active_ids:
                vc = self._channels[vid]
                self._quantum_remaining[vid] = int(self.config.wrr_quantum * vc.weight)

        # All empty
        return None

    def get_fairness_index(self) -> float:
        """Jain's Fairness Index across VC service counts."""
        counts = [vc.total_serviced for vc in self._channels.values()]
        if not counts or all(c == 0 for c in counts):
            return 1.0
        n = len(counts)
        s = sum(counts)
        ss = sum(c * c for c in counts)
        if ss == 0:
            return 1.0
        return (s * s) / (n * ss)

    @property
    def total_dropped(self) -> int:
        return sum(vc.total_dropped for vc in self._channels.values())

    @property
    def stats(self) -> Dict[str, object]:
        return {
            "total_decisions": self._total_decisions,
            "total_dropped": self.total_dropped,
            "fairness_index": self.get_fairness_index(),
            "per_channel": {
                vc.channel_id: {
                    "host_id": vc.host_id,
                    "enqueued": vc.total_enqueued,
                    "serviced": vc.total_serviced,
                    "dropped": vc.total_dropped,
                    "fill_level": vc.fill_level,
                }
                for vc in self._channels.values()
            },
        }


class MultiHostEndpointSimulator:
    """End-to-end simulator for multi-host descriptor batching + VC arbitration.

    Simulates the full flow:
      1. Each host assembles a Batch Descriptor Block
      2. Single doorbell triggers DMA pull into per-host VC
      3. WRR arbiter services VCs fairly
      4. Pipeline processes descriptors (score lookup + threshold + DMA)

    Target: P99 admission latency < 300 ns under 8-host contention
    (vs original 1.8 μs = 1800 ns).
    """

    def __init__(self, config: MultiHostEndpointConfig):
        self.config = config
        self.batcher = DescriptorBatcher(config.batch)
        self.arbiter = WRRArbiter(config.vc)
        self._backpressure_events = 0

    def setup_hosts(self, n_hosts: int, weights: Optional[List[float]] = None) -> None:
        """Register N hosts with their Virtual Channels."""
        if weights is None:
            weights = [1.0] * n_hosts
        for i in range(n_hosts):
            self.arbiter.register_channel(
                channel_id=i, host_id=i, weight=weights[i]
            )

    def simulate_step(
        self,
        host_descriptors: Dict[int, List[Dict]],
        current_time_ns: float = 0.0,
    ) -> MultiHostLatencyResult:
        """Simulate one decode step with all hosts submitting concurrently.

        Args:
            host_descriptors: host_id → list of descriptors to submit
            current_time_ns: current simulation time

        Returns:
            MultiHostLatencyResult with per-host and aggregate latencies.
        """
        cfg = self.config
        all_latencies: Dict[int, List[float]] = {
            h: [] for h in host_descriptors.keys()
        }

        # Phase 1: Each host creates and submits a batch
        submission_times: Dict[int, float] = {}
        for host_id, descs in host_descriptors.items():
            batch, submit_latency = self.batcher.create_batch(
                host_id, descs, current_time_ns
            )
            submission_times[host_id] = current_time_ns + submit_latency

            # Enqueue batch into VC
            accepted = self.arbiter.enqueue_batch(host_id, batch)
            if accepted < len(descs):
                self._backpressure_events += 1

        # Phase 2: Arbiter services all VCs until empty
        service_time_ns = max(submission_times.values()) if submission_times else current_time_ns
        pipeline_ns = cfg.pipeline_stages * cfg.cycle_ns

        total_processed = 0
        while True:
            decision = self.arbiter.arbitrate()
            if decision is None:
                break

            # Each descriptor takes pipeline_stages cycles through the endpoint
            service_time_ns += pipeline_ns
            total_processed += 1

            # Compute per-descriptor latency (from submission to service)
            host_submit = submission_times.get(decision.host_id, current_time_ns)
            desc_latency = service_time_ns - host_submit
            all_latencies[decision.host_id].append(desc_latency)

        # Compute metrics
        per_host_mean = {}
        per_host_p99 = {}
        per_host_max = {}
        all_flat = []

        for host_id, lats in all_latencies.items():
            if lats:
                arr = np.array(lats)
                per_host_mean[host_id] = float(arr.mean())
                per_host_p99[host_id] = float(np.percentile(arr, 99))
                per_host_max[host_id] = float(arr.max())
                all_flat.extend(lats)
            else:
                per_host_mean[host_id] = 0.0
                per_host_p99[host_id] = 0.0
                per_host_max[host_id] = 0.0

        if all_flat:
            all_arr = np.array(all_flat)
            agg_mean = float(all_arr.mean())
            agg_p99 = float(np.percentile(all_arr, 99))
            agg_max = float(all_arr.max())
        else:
            agg_mean = agg_p99 = agg_max = 0.0

        # Throughput
        total_time_ns = service_time_ns - current_time_ns
        throughput = total_processed / max(total_time_ns / 1000.0, 1e-9)  # per μs

        return MultiHostLatencyResult(
            per_host_mean_ns=per_host_mean,
            per_host_p99_ns=per_host_p99,
            per_host_max_ns=per_host_max,
            aggregate_mean_ns=agg_mean,
            aggregate_p99_ns=agg_p99,
            aggregate_max_ns=agg_max,
            fairness_index=self.arbiter.get_fairness_index(),
            total_descriptors_processed=total_processed,
            total_dropped=self.arbiter.total_dropped,
            backpressure_events=self._backpressure_events,
            throughput_descriptors_per_us=throughput,
        )

    def run_multi_step_simulation(
        self,
        n_hosts: int,
        descs_per_host: int = 25,
        n_steps: int = 100,
        seed: int = 42,
    ) -> Dict[str, object]:
        """Run multi-step simulation and collect statistics."""
        self.setup_hosts(n_hosts)
        rng = np.random.default_rng(seed)

        step_results = []
        for step in range(n_steps):
            # Each host submits descs_per_host descriptors
            host_descs = {}
            for h in range(n_hosts):
                host_descs[h] = [
                    {"chunk_id": f"h{h}_s{step}_c{i}", "priority": rng.integers(0, 255)}
                    for i in range(descs_per_host)
                ]

            result = self.simulate_step(host_descs, current_time_ns=step * 1e6)
            step_results.append(result)

        # Aggregate across steps
        all_means = [r.aggregate_mean_ns for r in step_results]
        all_p99 = [r.aggregate_p99_ns for r in step_results]

        return {
            "n_hosts": n_hosts,
            "descs_per_host": descs_per_host,
            "n_steps": n_steps,
            "mean_latency_ns": float(np.mean(all_means)),
            "p99_latency_ns": float(np.percentile(all_p99, 99)),
            "max_latency_ns": float(np.max([r.aggregate_max_ns for r in step_results])),
            "avg_fairness_index": float(np.mean([r.fairness_index for r in step_results])),
            "total_dropped": sum(r.total_dropped for r in step_results),
            "backpressure_events": sum(r.backpressure_events for r in step_results),
            "avg_throughput_per_us": float(np.mean(
                [r.throughput_descriptors_per_us for r in step_results]
            )),
            "arbiter_stats": self.arbiter.stats,
        }
