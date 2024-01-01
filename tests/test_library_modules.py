"""Smoke tests for the SimCXL-extension library modules.

These exercise the BDB / VC-WRR descriptor batcher, the multi-tenant fairness
allocator, and the per-VC CXL queue model end-to-end, so every shipped module
is executed by the test suite (not merely importable).
"""
import numpy as np
import pytest


def test_descriptor_batching_multihost_runs():
    from simcxl_ext.descriptor_batching import (
        MultiHostEndpointConfig, MultiHostEndpointSimulator,
    )
    sim = MultiHostEndpointSimulator(MultiHostEndpointConfig())
    out = sim.run_multi_step_simulation(n_hosts=4, descs_per_host=25,
                                        n_steps=20, seed=1)
    assert out["n_hosts"] == 4
    assert out["p99_latency_ns"] > 0
    # VC-WRR keeps tenants balanced.
    assert 0.0 <= out["avg_fairness_index"] <= 1.0 + 1e-9


def test_multi_tenant_fair_allocation_runs():
    from simcxl_ext.multi_tenant import (
        MultiTenantSimulator, TenantState, AllocationPolicy,
    )
    sim = MultiTenantSimulator()
    tenants = [
        TenantState(tenant_id=f"t{i}", model_name="Qwen2.5-7B",
                    context_length=32768, kv_budget_bytes=512 * 1024 * 1024)
        for i in range(4)
    ]
    res = sim.simulate(tenants, num_steps=20,
                       allocation_policy=AllocationPolicy.MAX_MIN_FAIRNESS)
    assert res is not None


def test_cxl_queue_simulator_payload_fetch():
    from simcxl_ext.cxl_queue_simulator import (
        CXLQueueSimulator, make_cxl_cxl20_config,
        make_cxl_asic_config, make_cxl_fpga_config,
    )
    # All three device configs construct.
    for make in (make_cxl_cxl20_config, make_cxl_asic_config, make_cxl_fpga_config):
        assert make() is not None
    q = CXLQueueSimulator(make_cxl_cxl20_config())
    result = q.submit_payload_fetch([0, 1, 2, 3], current_time_ns=0.0)
    stats = q.end_step()
    assert result is not None
    assert stats is not None
