"""Inherited SimCXL core: calibrated CXL.mem timing and protocol constants.

These are the parameters PROSE-APEX inherits **unchanged** from the
hardware-calibrated SimCXL full-system simulator (Cohet et al., HPCA'26),
validated against real CXL silicon: CXL.mem protocol-processing latency, bridge
transit, link bandwidth/latency, DDR5 timing, flit size, and queue depths.

The PROSE-APEX additions (MMIO-ring modelling, per-VC endpoint queues,
copy-engine scheduling, CXL.mem payload timing) live in the sibling modules
(``endpoint_sim``, ``descriptor_batching``, ``cxl_queue_simulator``,
``multi_tenant``, ``cxl_admission_sim``); see ``docs/SIMCXL_EXTENSION.md`` for
the inherited-vs-added split and the calibration source of every value.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SimCXLTiming:
    """Validated SimCXL timing parameters (inherited from the calibrated core)."""
    clock_period_ns: float = 1.0               # 1 GHz reference clock
    proto_proc_lat_ns: float = 15.0            # CXL.mem protocol processing
    bridge_lat_ns: float = 50.0                # CXL bridge transit
    cxl_link_bw_gbps: float = 55.0             # CXL 2.0 x16
    cxl_link_latency_ns: float = 250.0         # one-way CXL link
    ddr5_tCL_ns: float = 16.0                  # DDR5-4400 CAS
    ddr5_tRCD_ns: float = 16.0                 # DDR5-4400 RAS-to-CAS
    ddr5_tRP_ns: float = 16.0                  # DDR5-4400 precharge
    ddr5_tRAS_ns: float = 32.0                 # DDR5-4400 row active
    ddr5_bandwidth_gbps: float = 70.4          # DDR5-4400 per channel
    hbm_bandwidth_gbps: float = 900.0          # HBM2e proxy (fast tier)
    cxl_flit_size_bytes: int = 256             # CXL.mem flit size
    cxl_protocol_overhead_pct: float = 2.0     # DBIE + CRC overhead
    resp_queue_depth: int = 48                 # response queue limit
    req_queue_depth: int = 48                  # request queue limit
    credit_rtt_ns: float = 100.0               # CXL credit return RTT
    l3_cacheline_bytes: int = 64               # cache line size


class CXLCmd:
    """CXL.mem sub-protocol commands (matching the SimCXL packet model)."""
    M2SReq = 0    # Master-to-Subordinate Request (read)
    M2SRwD = 1    # Master-to-Subordinate Request with Data (write)
    S2MDRS = 2    # Subordinate-to-Master Data Response (read response)
    S2MNDR = 3    # Subordinate-to-Master No Data Response (write response)
