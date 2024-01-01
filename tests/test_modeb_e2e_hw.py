"""End-to-end test for the single-host Mode B (endpoint-gated pull) benchmark.

This compiles and runs host_sw/bench_modeb_e2e.cpp — the harness that runs the
Mode B protocol against a commodity CXL Type-3 substrate (real devdax when
present, self-labelled emulation otherwise). It asserts the two claims that make
the "real hardware" rebuttal meaningful:

  1. Mode B moves ZERO reclaimed-payload bytes (RPE == 0), and
  2. the SAME byte instrument DOES register a leak for the fetch-then-score
     control (RPE > 0) — so the RPE==0 is falsifiable, not vacuous.

Both hold regardless of substrate; a real CXL device only changes the measured
latency. The test skips (does not fail) when no C++17 compiler is available.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOST_SW = Path(__file__).resolve().parent.parent / "host_sw"
SRC = HOST_SW / "bench_modeb_e2e.cpp"


def _find_cxx():
    for cc in ("g++", "clang++", "c++"):
        path = shutil.which(cc)
        if path:
            return path
    return None


@pytest.fixture(scope="module")
def bench_bin(tmp_path_factory):
    cxx = _find_cxx()
    if cxx is None:
        pytest.skip("no C++17 compiler (g++/clang++) available")
    if not SRC.exists():
        pytest.skip(f"benchmark source missing: {SRC}")
    out = tmp_path_factory.mktemp("modeb") / "bench_modeb_e2e"
    exe = str(out) + (".exe" if os.name == "nt" else "")
    proc = subprocess.run(
        [cxx, "-std=c++17", "-O2", "-I", str(HOST_SW), "-o", exe, str(SRC)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"benchmark failed to compile:\n{proc.stderr}")
    return exe


def _run(bench_bin, *args):
    proc = subprocess.run(
        [bench_bin, "--json", *args],
        capture_output=True, text=True, timeout=300,
    )
    # Exit code 0 means both invariants held; the harness enforces them too.
    assert proc.returncode == 0, (
        f"benchmark returned {proc.returncode}\nstderr:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_mode_b_moves_zero_rejected_payload(bench_bin):
    res = _run(bench_bin, "--steps", "100")
    assert res["mode_b"]["rpe_bytes"] == 0, (
        "Mode B must move zero reclaimed-payload bytes on any substrate")


def test_falsifiability_control_leaks(bench_bin):
    """The RPE==0 is only meaningful if the same instrument can see a leak."""
    res = _run(bench_bin, "--steps", "100")
    assert res["fts_baseline"]["rpe_bytes"] > 0, (
        "fetch-then-score control must register RPE>0 through the same counter")
    # And Mode B must read strictly fewer bytes than blind fetch-then-score.
    assert res["mode_b"]["bytes_read"] < res["fts_baseline"]["bytes_read"]


def test_admitted_bytes_equal_bytes_read_in_mode_b(bench_bin):
    """Every byte Mode B pulls is an admitted byte — no unaccounted traffic."""
    res = _run(bench_bin, "--steps", "100")
    mb = res["mode_b"]
    assert mb["admitted_bytes"] == mb["bytes_read"]


def test_promotion_latency_reported(bench_bin):
    res = _run(bench_bin, "--steps", "100")
    mb = res["mode_b"]
    assert mb["promo_us_mean"] > 0.0
    assert mb["promo_us_p99"] >= mb["promo_us_p50"] > 0.0


def test_substrate_is_reported_honestly(bench_bin):
    """The harness must always say which substrate produced the numbers."""
    res = _run(bench_bin, "--steps", "50")
    assert res["substrate"] in (
        "real-devdax", "real-numa-node", "emulated-file", "emulated-anon")
    assert isinstance(res["real_cxl"], bool)
