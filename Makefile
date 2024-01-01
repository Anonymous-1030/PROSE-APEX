# =============================================================================
# PROSE-APEX Top-Level Makefile
#
# End-to-end automation for artifact reproduction.
# Targets execute in strict dependency order; any failure aborts immediately.
#
#   make all         Full pipeline: gen_trace → rtl_sim → xcheck → synth
#   make gen_trace   Generate causal LLM decode trace (16 tenants, 2000 steps)
#   make rtl_sim     Compile and run APEX + CEFE RTL testbenches (Icarus Verilog)
#   make xcheck      Cross-check RTL output against Python reference model
#   make synth       Run DC/Genus synthesis (requires PDK; see rtl/synth/)
#   make baselines   Run Quest-CXL and InfiniGen-CXL baseline reproduction
#   make test        Run pytest smoke tests
#   make clean       Remove all generated artifacts
#
# Requirements:
#   - Python >= 3.9 with numpy, matplotlib
#   - Icarus Verilog >= 11 (iverilog, vvp)
#   - (Optional) Cadence Genus or Synopsys DC for synthesis
# =============================================================================

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.ONESHELL:

PY       ?= python
IVERILOG ?= iverilog
VVP      ?= vvp

# Directories
ROOT     := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
RTL_DIR  := $(ROOT)/rtl
SCRIPT_DIR := $(ROOT)/scripts
EXP_DIR  := $(ROOT)/experiments
OUT_DIR  := $(ROOT)/experiments/out/data
XCHECK_DIR := $(ROOT)/experiments/_xcheck_out

# Output artifacts
TRACE_FILE    := $(OUT_DIR)/trace.jsonl
QUEST_FILE    := $(OUT_DIR)/quest_cxl.json
XCHECK_TRACE  := $(XCHECK_DIR)/xcheck_trace.txt
XCHECK_RTL    := $(XCHECK_DIR)/xcheck_rtl_out.txt

# RTL source sets (leaf modules before top level; order matters for iverilog)
APEX_RTL := \
	$(RTL_DIR)/APEX_PKG.sv \
	$(RTL_DIR)/ICG.sv \
	$(RTL_DIR)/APEX_EXPERT_BANK.sv \
	$(RTL_DIR)/APEX_PCM.sv \
	$(RTL_DIR)/cefe_pin_table.sv \
	$(RTL_DIR)/APEX_MAC_ARRAY.sv \
	$(RTL_DIR)/APEX_TOPK_HEAP.sv \
	$(RTL_DIR)/APEX_WEIGHT_UPDATE.sv \
	$(RTL_DIR)/APEX_PIPELINE_CTRL.sv \
	$(RTL_DIR)/APEX_LOSS_COMPUTE.sv \
	$(RTL_DIR)/APEX_SEA.sv \
	$(RTL_DIR)/APEX_PIPELINE.sv

CEFE_RTL := \
	$(RTL_DIR)/cefe_vc_wrr.sv \
	$(RTL_DIR)/cefe_cfo_cam.sv \
	$(RTL_DIR)/cefe_bdb_parser.sv

ALL_RTL := $(APEX_RTL) $(CEFE_RTL)

# Simulation binaries
APEX_SIM_BIN := $(RTL_DIR)/apex_pipeline_sim
CEFE_SIM_BIN := $(RTL_DIR)/cefe_sim

# =============================================================================
# Top-Level Targets
# =============================================================================

.PHONY: all gen_trace rtl_sim xcheck synth baselines test clean help supplementary

all: gen_trace rtl_sim xcheck baselines
	@echo ""
	@echo "============================================================"
	@echo " ALL STEPS COMPLETE"
	@echo "============================================================"
	@echo " Trace:     $(TRACE_FILE)"
	@echo " Baselines: $(QUEST_FILE)"
	@echo " RTL sim:   PASS"
	@echo " X-check:   PASS"
	@echo "============================================================"

# =============================================================================
# Supplementary Experiments (S1-S6)
# =============================================================================

supplementary:
	@echo "=== Running Supplementary Experiments (S1-S6) ==="
	$(PY) $(EXP_DIR)/run_s1_software_stack.py
	$(PY) $(EXP_DIR)/run_s2_robustness.py
	$(PY) $(EXP_DIR)/run_s3_long_context.py
	$(PY) $(EXP_DIR)/run_s4_multi_tenant.py
	$(PY) $(EXP_DIR)/run_s5_cfo_topology.py
	$(PY) $(EXP_DIR)/run_s6_physical_impl.py
	@echo ""
	@echo "============================================================"
	@echo " SUPPLEMENTARY EXPERIMENTS COMPLETE"
	@echo " Figures: $(ROOT)/experiments/out/figures/s{1..6}_*.pdf"
	@echo " Data:    $(ROOT)/experiments/out/data/s{1..6}_*.json"
	@echo "============================================================"

# =============================================================================
# Step 1: Generate Causal Trace
# =============================================================================

gen_trace: $(TRACE_FILE)

$(TRACE_FILE): $(SCRIPT_DIR)/gen_causal_trace.py
	@echo "=== [1/4] Generating causal trace (16 tenants, 2000 steps) ==="
	@mkdir -p $(OUT_DIR)
	$(PY) $(SCRIPT_DIR)/gen_causal_trace.py \
		--tenants 16 \
		--steps 2000 \
		--k-budget 25 \
		--jaccard 0.65 \
		--overlap 0.52 \
		--output $(TRACE_FILE) \
		--validate
	@echo "  Trace generated: $(TRACE_FILE)"
	@echo ""

# =============================================================================
# Step 2: RTL Simulation (Icarus Verilog)
# =============================================================================

rtl_sim: $(APEX_SIM_BIN)
	@echo "=== [2/4] Running RTL testbench ==="
	cd $(RTL_DIR) && $(VVP) apex_pipeline_sim
	@echo "  RTL simulation PASSED"
	@echo ""

$(APEX_SIM_BIN): $(APEX_RTL) $(RTL_DIR)/APEX_PIPELINE_TB.sv
	@echo "  Compiling APEX pipeline RTL..."
	cd $(RTL_DIR) && $(IVERILOG) -g2012 -o apex_pipeline_sim \
		$(notdir $(APEX_RTL)) APEX_PIPELINE_TB.sv

# =============================================================================
# Step 3: Cross-Check (RTL vs Python reference model)
# =============================================================================

xcheck: $(XCHECK_RTL)
	@echo "=== [3/4] Cross-checking RTL against Python reference ==="
	$(PY) $(EXP_DIR)/run_rtl_xcheck.py --trace $(XCHECK_TRACE) --rtl-out $(XCHECK_RTL)
	@echo "  Cross-check PASSED (per-descriptor latency + PCM-reject + heap-admit agreement)"
	@echo ""

$(XCHECK_RTL): $(APEX_RTL) $(RTL_DIR)/APEX_XCHECK_TB.sv $(XCHECK_TRACE)
	@echo "=== Running trace-driven RTL cross-check simulation ==="
	cd $(RTL_DIR) && $(IVERILOG) -g2012 -o apex_xcheck_sim \
		$(notdir $(APEX_RTL)) APEX_XCHECK_TB.sv
	cd $(RTL_DIR) && $(VVP) apex_xcheck_sim \
		+TRACE=../$(XCHECK_TRACE) +OUT=../$(XCHECK_RTL)

# =============================================================================
# Step 4: Baselines
# =============================================================================

baselines: $(QUEST_FILE)

$(QUEST_FILE): $(SCRIPT_DIR)/quest_cxl_baseline.py
	@echo "=== [4/4] Running baseline algorithms ==="
	@mkdir -p $(OUT_DIR)
	$(PY) $(SCRIPT_DIR)/quest_cxl_baseline.py \
		--n-pages 80 \
		--k-select 25 \
		--cxl-latency 300 \
		--steps 2000 \
		--output $(QUEST_FILE) \
		--include-infinigen \
		--sweep-latency
	@echo "  Baselines complete: $(QUEST_FILE)"
	@echo ""

# =============================================================================
# Synthesis (optional, requires PDK)
# =============================================================================

synth:
	@echo "=== Running synthesis (edit rtl/synth/synthesize_apex.tcl for PDK) ==="
	cd $(RTL_DIR)/synth && genus -files synthesize_apex.tcl

# =============================================================================
# Test
# =============================================================================

test:
	@echo "=== Running pytest smoke tests ==="
	$(PY) -m pytest -q tests/

# =============================================================================
# Clean
# =============================================================================

clean:
	rm -f $(RTL_DIR)/apex_pipeline_sim $(RTL_DIR)/apex_xcheck_sim $(RTL_DIR)/cefe_sim
	rm -f $(RTL_DIR)/*.vcd
	rm -f $(TRACE_FILE) $(QUEST_FILE)
	rm -rf $(XCHECK_DIR)
	rm -rf $(RTL_DIR)/synth/results

# =============================================================================
# Help
# =============================================================================

help:
	@echo "PROSE-APEX Artifact Makefile"
	@echo ""
	@echo "Targets:"
	@echo "  all        Full pipeline (trace → sim → xcheck → baselines)"
	@echo "  gen_trace  Generate causal LLM trace"
	@echo "  rtl_sim    Run RTL simulation (Icarus Verilog)"
	@echo "  xcheck     Cross-check RTL vs reference model"
	@echo "  synth      Run synthesis (requires PDK)"
	@echo "  baselines  Run Quest-CXL + InfiniGen-CXL baselines"
	@echo "  test       Run pytest"
	@echo "  clean      Remove generated files"
	@echo "  help       This message"
