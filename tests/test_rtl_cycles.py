"""Smoke test for the CEFE / PPU-APEX RTL.

Compiles the APEX admission pipeline with Icarus Verilog and runs the
testbench, asserting the 9-cycle admit / 4-cycle reject latency and a clean
8/8 pass (7 verification tests, drain protocol counts 2 sub-checks). The admit
path is the 8-stage internal pipeline plus the S8 shared-MMIO completion
register; the reject bypass is 4 cycles.
The top-K module implements the dual-zone exact O(1)-depth design (7-entry
EZ min-heap + 18-entry SZ flat array, three-branch admission), with the Case 2
single-cycle safe_min stall restored.
Skipped when iverilog is not installed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

RTL_DIR = Path(__file__).resolve().parent.parent / "rtl"
RTL_CORE = [
    "APEX_PKG.sv", "ICG.sv", "APEX_EXPERT_BANK.sv", "APEX_PCM.sv",
    "cefe_pin_table.sv",
    "APEX_MAC_ARRAY.sv", "APEX_TOPK_HEAP.sv", "APEX_WEIGHT_UPDATE.sv",
    "APEX_PIPELINE_CTRL.sv", "APEX_LOSS_COMPUTE.sv", "APEX_SEA.sv",
    "APEX_PIPELINE.sv", "APEX_PIPELINE_TB.sv",
]

# Standalone module testbenches (Invariant 1, long-context mapping, Mode A DMA).
PIN_TB    = ["APEX_PKG.sv", "cefe_pin_table.sv", "cefe_pin_table_tb.sv"]
MAPPER_TB = ["cefe_addr_mapper.sv", "cefe_addr_mapper_tb.sv"]
DMA_TB    = ["APEX_PKG.sv", "cefe_dma_engine.sv", "cefe_dma_engine_tb.sv"]


def _run_tb(tmp_path_factory, name, files):
    out = tmp_path_factory.mktemp(name)
    binary = out / (name + "_sim")
    compile_cmd = ["iverilog", "-g2012", "-o", str(binary)] + [
        str(RTL_DIR / f) for f in files
    ]
    res = subprocess.run(compile_cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, f"iverilog compile failed:\n{res.stderr}"
    run = subprocess.run(["vvp", str(binary)], capture_output=True, text=True,
                         cwd=str(out), encoding="utf-8", errors="replace")
    return run.stdout + run.stderr

pytestmark = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="Icarus Verilog (iverilog/vvp) not installed",
)


@pytest.fixture(scope="module")
def sim_output(tmp_path_factory):
    out = tmp_path_factory.mktemp("apex_rtl")
    binary = out / "apex_sim"
    compile_cmd = ["iverilog", "-g2012", "-o", str(binary)] + [
        str(RTL_DIR / f) for f in RTL_CORE
    ]
    # iverilog emits "sorry:" notices for unsupported (but harmless) constructs;
    # it still produces a working binary and returns 0.
    res = subprocess.run(compile_cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, f"iverilog compile failed:\n{res.stderr}"
    assert binary.exists(), "iverilog produced no simulation binary"
    run = subprocess.run(["vvp", str(binary)], capture_output=True, text=True,
                         cwd=str(out), encoding="utf-8", errors="replace")
    return run.stdout + run.stderr


def test_all_testbench_checks_pass(sim_output):
    assert "ALL TESTS PASSED" in sim_output, sim_output
    assert "0 FAIL" in sim_output, sim_output


def test_admit_path_is_nine_cycles(sim_output):
    assert "Admitted in 9 cycles" in sim_output, sim_output


def test_reject_path_is_four_cycles(sim_output):
    assert "Rejected in 4 cycles" in sim_output, sim_output


def test_residency_reject_is_four_cycles(sim_output):
    assert "Residency reject in 4 cycles" in sim_output, sim_output


def test_rpe_zero_no_payload_for_invalid(sim_output):
    # The RTL testbench streams the three invalid classes (bad epoch, bad
    # namespace, resident) and monitors dma_valid directly: RPE=0 means every
    # invalid descriptor is rejected AND zero DMA payload beats are observed.
    assert "(RPE=0)" in sim_output, sim_output
    assert "dma_valid beats=0" in sim_output, sim_output


def test_pin_blocks_reclaim_invariant1(sim_output):
    # The integrated pipeline TB (Test 9) checks that an in-flight transfer's
    # (chunk, generation) binding cannot be reclaimed while pinned, and can be
    # once released — the hardware form of Invariant 1.
    assert "OAT pin blocks reclaim" in sim_output, sim_output


@pytest.fixture(scope="module")
def pin_tb_output(tmp_path_factory):
    return _run_tb(tmp_path_factory, "apex_pin", PIN_TB)


def test_pin_table_standalone_invariant(pin_tb_output):
    # cefe_pin_table_tb: reclaim forbidden while pinned, generation-exact,
    # allowed after RELEASE, and alloc_ok=0 on a full table (OAT must reject).
    assert "0 errors" in pin_tb_output, pin_tb_output


@pytest.fixture(scope="module")
def mapper_tb_output(tmp_path_factory):
    return _run_tb(tmp_path_factory, "apex_mapper", MAPPER_TB)


def test_addr_mapper_two_tier(mapper_tb_output):
    # cefe_addr_mapper_tb: single-cycle Tier-1 hit, causal write discipline,
    # 3-cycle Tier-2 backing probe, tag-validated zero fallback.
    assert "0 errors" in mapper_tb_output, mapper_tb_output


@pytest.fixture(scope="module")
def dma_tb_output(tmp_path_factory):
    return _run_tb(tmp_path_factory, "apex_dma", DMA_TB)


def test_dma_engine_mode_a(dma_tb_output):
    # cefe_dma_engine_tb: P2P posted-write streaming, PASID tagging,
    # credit-based flow control, completion->RELEASE(d) timing.
    assert "0 errors" in dma_tb_output, dma_tb_output
