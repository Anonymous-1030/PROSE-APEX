#!/usr/bin/env bash
# One-shot verification: pytest + RTL simulation + all reproduction experiments.
# Run from the artifact root:  bash scripts/verify.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PY="${PYTHON:-python}"

echo "==================================================================="
echo " 1/3  pytest smoke tests"
echo "==================================================================="
"$PY" -m pytest -q tests/

echo
echo "==================================================================="
echo " 2/3  RTL testbench (Icarus Verilog)"
echo "==================================================================="
if command -v iverilog >/dev/null 2>&1; then
    ( cd rtl && make --no-print-directory sim )
else
    echo "SKIP: iverilog not found on PATH (install Icarus Verilog >= 11)."
fi

echo
echo "==================================================================="
echo " 3/3  Reproduction experiments"
echo "==================================================================="
"$PY" experiments/run_rpe_ordering.py
echo
"$PY" experiments/run_simcxl_multihost.py
echo
"$PY" experiments/run_cfo_overlap.py

echo
echo "All verification steps complete."
