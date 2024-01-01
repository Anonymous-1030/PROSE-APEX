"""Cycle-level CXL-endpoint descriptor-burst simulator.

This is the endpoint-pipeline model behind the multi-host admission-latency
numbers in the paper (§IV-D Multi-host scalability): it tracks the
descriptor ring, the scorer pipeline (fast-path admit vs reject / null
completion), DMA-initiation back-pressure, and per-descriptor queuing delay.

The per-descriptor admit / reject cycle counts (``fast_path_cycles`` = 8,
``reject_path_cycles`` = 3) are the SimCXL-model counts that the synthesizable
APEX RTL is cross-checked against (9 / 4 cycles including the shared MMIO
dequeue stage; see ../rtl). All other parameters are documented in
``docs/SIMCXL_EXTENSION.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import numpy as np


@dataclass
class EndpointConfig:
    """CXL endpoint hardware configuration."""
    descriptor_ring_depth: int = 256
    completion_ring_depth: int = 256
    scorer_pipeline_stages: int = 4
    scorer_clock_mhz: float = 1500.0   # 1.5 GHz endpoint scorer clock
    fast_path_cycles: int = 8          # admit path (matches RTL datapath)
    reject_path_cycles: int = 4        # reject path (matches RTL datapath)
    null_completion_cycles: int = 2
    metadata_read_ns: float = 110.0
    dma_initiation_cycles: int = 4
    max_outstanding_dma: int = 16
    backpressure_threshold: float = 0.85  # ring occupancy that asserts backpressure
    # Mode B (Pull) configuration
    mode: str = "Mode_A_Push"          # "Mode_A_Push" or "Mode_B_Pull"
    pull_token_capacity: int = 64      # max outstanding reservations
    pull_token_expiry_cycles: int = 75000  # ~50 µs at 1.5 GHz
    pull_host_rtt_cycles: int = 225    # ~150 ns at 1.5 GHz
    pull_cfo_enabled: bool = True      # CFO coalescing in pull mode


@dataclass
class DescriptorBurst:
    """A burst of descriptors submitted to the endpoint by one tenant."""
    n_descriptors: int
    inter_arrival_ns: float
    tenant_id: int
    priority: int = 0
    is_phase_shift: bool = False


@dataclass
class EndpointState:
    """Mutable state of the endpoint pipeline, carried across bursts."""
    cycle: int = 0
    desc_ring_occupancy: int = 0
    comp_ring_occupancy: int = 0
    outstanding_dma: int = 0
    backpressure_active: bool = False
    total_admitted: int = 0
    total_rejected: int = 0
    total_null_completions: int = 0
    total_backpressure_cycles: int = 0
    total_stall_cycles: int = 0
    latencies: List[float] = field(default_factory=list)
    per_tenant_admitted: Dict[int, int] = field(default_factory=dict)
    per_tenant_rejected: Dict[int, int] = field(default_factory=dict)
    fault_events: List[Dict[str, Any]] = field(default_factory=list)
    # Mode B state
    active_tokens: List[Dict[str, Any]] = field(default_factory=list)
    total_token_issued: int = 0
    total_token_expired: int = 0
    total_token_redeemed: int = 0
    total_pull_rpe: int = 0  # must remain 0 (structural guarantee)


def simulate_endpoint_burst(
    burst: DescriptorBurst,
    config: EndpointConfig,
    state: EndpointState,
    scorer_accept_rate: float = 0.6,
    rng: "np.random.Generator | None" = None,
) -> EndpointState:
    """Process one descriptor burst through the endpoint pipeline.

    Models the descriptor ring fill/drain, the scorer accept/reject decision,
    null completion for rejects, DMA initiation for admits, back-pressure when
    rings fill, and per-descriptor queuing delay. Returns the mutated state so
    successive bursts (e.g. one per host) accumulate contention.
    """
    if rng is None:
        rng = np.random.default_rng(42)

    cycle_ns = 1000.0 / config.scorer_clock_mhz
    inter_arrival_cycles = max(1, int(burst.inter_arrival_ns / cycle_ns))
    is_pull = (config.mode == "Mode_B_Pull")

    for desc_idx in range(burst.n_descriptors):
        # Ring drains at one descriptor per fast_path_cycles (pipeline rate).
        drain_per_arrival = inter_arrival_cycles / config.fast_path_cycles
        state.desc_ring_occupancy = max(
            0, state.desc_ring_occupancy - int(drain_per_arrival)
        )

        ring_util = state.desc_ring_occupancy / config.descriptor_ring_depth
        if ring_util >= config.backpressure_threshold:
            state.backpressure_active = True
            stall = int(config.fast_path_cycles * 3)
            state.total_stall_cycles += stall
            state.total_backpressure_cycles += stall
            state.cycle += stall
            state.desc_ring_occupancy = int(config.descriptor_ring_depth * 0.5)
        else:
            state.backpressure_active = False

        state.desc_ring_occupancy += 1

        # Expire old tokens (Mode B)
        if is_pull and state.active_tokens:
            state.active_tokens = [
                t for t in state.active_tokens
                if state.cycle - t["issued_cycle"] < config.pull_token_expiry_cycles
            ]
            expired_count = state.total_token_issued - state.total_token_expired - state.total_token_redeemed - len(state.active_tokens)
            if expired_count > 0:
                state.total_token_expired += expired_count

        accept = rng.random() < scorer_accept_rate
        if accept:
            state.total_admitted += 1
            state.per_tenant_admitted[burst.tenant_id] = (
                state.per_tenant_admitted.get(burst.tenant_id, 0) + 1
            )

            if is_pull:
                # Mode B: issue reservation token, no DMA yet
                latency_cycles = config.fast_path_cycles
                token = {
                    "chunk_id": desc_idx,
                    "tenant_id": burst.tenant_id,
                    "issued_cycle": state.cycle,
                }
                state.active_tokens.append(token)
                state.total_token_issued += 1
                # Host pull arrives after RTT; simulate token redemption
                pull_arrive_cycle = state.cycle + config.pull_host_rtt_cycles
                if pull_arrive_cycle - token["issued_cycle"] < config.pull_token_expiry_cycles:
                    # Token still valid → serve read, no RPE
                    state.total_token_redeemed += 1
                    latency_cycles += config.pull_host_rtt_cycles
                else:
                    # Token expired → reject read → no payload transferred → RPE=0
                    state.total_token_expired += 1
                    latency_cycles += config.pull_host_rtt_cycles
                    # No DMA issued, no payload moved: RPE remains 0
            else:
                # Mode A: immediate DMA programming
                latency_cycles = config.fast_path_cycles + config.dma_initiation_cycles
                state.outstanding_dma += 1
                if state.outstanding_dma > config.max_outstanding_dma:
                    dma_stall = config.fast_path_cycles * 2
                    latency_cycles += dma_stall
                    state.total_stall_cycles += dma_stall
                    state.outstanding_dma = config.max_outstanding_dma
        else:
            latency_cycles = config.reject_path_cycles + config.null_completion_cycles
            state.total_rejected += 1
            state.per_tenant_rejected[burst.tenant_id] = (
                state.per_tenant_rejected.get(burst.tenant_id, 0) + 1
            )
            state.total_null_completions += 1

        # Queuing delay from ring depth (M/D/1 approximation).
        if state.desc_ring_occupancy > config.scorer_pipeline_stages:
            queue_wait = (state.desc_ring_occupancy - config.scorer_pipeline_stages) * 2
            latency_cycles += queue_wait

        state.comp_ring_occupancy += 1
        if state.comp_ring_occupancy >= config.completion_ring_depth:
            state.comp_ring_occupancy = config.completion_ring_depth // 2
            state.total_stall_cycles += 4

        state.latencies.append(latency_cycles * cycle_ns)
        state.cycle += inter_arrival_cycles

        # DMA completions drain asynchronously.
        if state.outstanding_dma > 0 and desc_idx % 4 == 0:
            state.outstanding_dma -= 1

    return state
