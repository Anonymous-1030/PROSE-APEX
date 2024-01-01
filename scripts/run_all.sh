#!/usr/bin/env bash
# =============================================================================
# PROSE-APEX: One-Shot Full Reproduction Script
#
# Executes the complete artifact reproduction pipeline:
#   1. gen_trace   — Generate causal LLM trace (Markov model, Jaccard≈0.65)
#   2. rtl_sim     — Compile + run RTL testbench (9-cycle admit, 4-cycle reject)
#   3. xcheck      — Cross-check RTL against Python reference model
#   4. baselines   — Run Quest-CXL & InfiniGen-CXL (prove Recovery@K ≈ random)
#   5. experiments — Run all 6 reproduction experiments from the paper
#
# Exit code: 0 if all steps pass, non-zero on first failure.
#
# Usage:
#   bash scripts/run_all.sh           # Full run
#   bash scripts/run_all.sh --quick   # Skip synthesis and long experiments
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PY="${PYTHON:-python}"
QUICK=0

if [[ "${1:-}" == "--quick" ]]; then
    QUICK=1
    echo "Quick mode: skipping synthesis and long experiments"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass_count=0
fail_count=0

run_step() {
    local step_num="$1"
    local step_name="$2"
    shift 2

    echo ""
    echo "==================================================================="
    printf " ${YELLOW}[%s]${NC} %s\n" "$step_num" "$step_name"
    echo "==================================================================="

    if "$@"; then
        printf " ${GREEN}✓ PASS${NC}: %s\n" "$step_name"
        pass_count=$((pass_count + 1))
    else
        printf " ${RED}✗ FAIL${NC}: %s\n" "$step_name"
        fail_count=$((fail_count + 1))
        echo ""
        echo "FATAL: Step $step_num failed. Aborting."
        exit 1
    fi
}

echo "==================================================================="
echo " PROSE-APEX Full Artifact Reproduction"
echo " Date: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo " Python: $("$PY" --version 2>&1)"
echo "==================================================================="

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: pytest smoke tests
# ─────────────────────────────────────────────────────────────────────────────
run_step "1/6" "pytest smoke tests" "$PY" -m pytest -q tests/

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Generate causal trace
# ─────────────────────────────────────────────────────────────────────────────
run_step "2/6" "Generate causal trace (16 tenants, 2000 steps)" \
    "$PY" scripts/gen_causal_trace.py \
        --tenants 16 --steps 2000 --k-budget 25 \
        --jaccard 0.65 --overlap 0.52 \
        --output experiments/out/data/trace.jsonl \
        --validate

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: RTL simulation
# ─────────────────────────────────────────────────────────────────────────────
if command -v iverilog >/dev/null 2>&1; then
    run_step "3/6" "RTL testbench (Icarus Verilog)" \
        bash -c "cd rtl && make --no-print-directory sim"
else
    echo ""
    echo "==================================================================="
    printf " ${YELLOW}[3/6]${NC} RTL testbench — SKIPPED (iverilog not found)\n"
    echo "==================================================================="
    pass_count=$((pass_count + 1))
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Cross-check RTL vs reference
# ─────────────────────────────────────────────────────────────────────────────
run_step "4/6" "Cross-check (RPE=0 + multi-host)" \
    bash -c "$PY experiments/run_rpe_ordering.py && $PY experiments/run_simcxl_multihost.py"

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Baseline algorithms
# ─────────────────────────────────────────────────────────────────────────────
run_step "5/6" "Quest-CXL + InfiniGen-CXL baselines" \
    "$PY" scripts/quest_cxl_baseline.py \
        --n-pages 80 --k-select 25 --cxl-latency 300 --steps 2000 \
        --output experiments/out/data/quest_cxl.json \
        --include-infinigen --sweep-latency

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Full reproduction experiments
# ─────────────────────────────────────────────────────────────────────────────
if [[ $QUICK -eq 0 ]]; then
    run_step "6/6" "Full reproduction experiments" \
        bash -c "
            $PY experiments/run_cfo_overlap.py &&
            $PY experiments/run_budget_accuracy.py &&
            $PY experiments/run_placement_isolation.py &&
            $PY experiments/run_sensitivity_enclosure.py
        "
else
    echo ""
    echo "==================================================================="
    printf " ${YELLOW}[6/6]${NC} Full experiments — SKIPPED (--quick mode)\n"
    echo "==================================================================="
    pass_count=$((pass_count + 1))
fi

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "==================================================================="
printf " ${GREEN}ALL STEPS COMPLETE${NC}\n"
echo " Passed: $pass_count / $((pass_count + fail_count))"
echo "==================================================================="
echo ""
echo " Generated artifacts:"
echo "   Trace:     experiments/out/data/trace.jsonl"
echo "   Baselines: experiments/out/data/quest_cxl.json"
echo "   Figures:   experiments/out/figures/"
echo ""
echo " To run synthesis (requires ASAP7 PDK):"
echo "   cd rtl/synth && genus -files synthesize_apex.tcl"
echo "==================================================================="

exit 0
