# =============================================================================
# Makefile for the CEFE / PPU-APEX admission-scoring RTL
#
#   make sim     Compile + run the testbench under Icarus Verilog (iverilog).
#                Verifies the 9-cycle admit / 4-cycle reject latency claims.
#   make synth   Run Cadence Genus (or Synopsys DC) synthesis on the datapath.
#                Requires an ASAP7 7nm (or compatible) standard-cell library; edit
#                synth/synthesize_apex.tcl to point at your PDK.
#   make wave    Open the dumped waveform in GTKWave.
#   make clean   Remove simulation byproducts.
#
# Tool versions used for the paper: Icarus Verilog 12.0; Synopsys DC / Cadence
# Genus against the ASAP7 7 nm library at 1 GHz.
# =============================================================================

IVERILOG ?= iverilog
VVP      ?= vvp
GTKWAVE  ?= gtkwave

# Synthesis order matters: leaf modules before the top level and testbench.
RTL_CORE = \
	APEX_PKG.sv \
	ICG.sv \
	APEX_EXPERT_BANK.sv \
	APEX_PCM.sv \
	cefe_pin_table.sv \
	APEX_MAC_ARRAY.sv \
	APEX_TOPK_HEAP.sv \
	APEX_WEIGHT_UPDATE.sv \
	APEX_PIPELINE_CTRL.sv \
	APEX_LOSS_COMPUTE.sv \
	APEX_SEA.sv \
	APEX_PIPELINE.sv

RTL_CEFE = \
	cefe_vc_wrr.sv \
	cefe_cfo_cam.sv \
	cefe_bdb_parser.sv \
	cefe_addr_mapper.sv \
	cefe_dma_engine.sv

RTL_TB   = APEX_PIPELINE_TB.sv

SIM_BIN  = apex_pipeline_sim
VCD      = apex_pipeline.vcd

.PHONY: sim synth wave clean cefe_check topk_oracle xcheck pin_check

sim: $(SIM_BIN)
	@echo "=== Running APEX pipeline testbench ==="
	$(VVP) $(SIM_BIN)

# Standalone pin-table invariant test (paper §III-B, Invariant 1).
pin_check:
	@echo "=== Running cefe_pin_table testbench (Invariant 1) ==="
	$(IVERILOG) -g2012 -o pin_tb_sim APEX_PKG.sv cefe_pin_table.sv cefe_pin_table_tb.sv
	$(VVP) pin_tb_sim

# Standalone long-context address-mapper test (paper §III-C.c).
mapper_check:
	@echo "=== Running cefe_addr_mapper testbench (two-tier mapping) ==="
	$(IVERILOG) -g2012 -o mapper_tb_sim cefe_addr_mapper.sv cefe_addr_mapper_tb.sv
	$(VVP) mapper_tb_sim

# Standalone Mode A DMA-engine test (paper §III-D, Table I).
dma_check:
	@echo "=== Running cefe_dma_engine testbench (P2P posted writes) ==="
	$(IVERILOG) -g2012 -o dma_tb_sim APEX_PKG.sv cefe_dma_engine.sv cefe_dma_engine_tb.sv
	$(VVP) dma_tb_sim

topk_oracle:
	@echo "=== Running Top-K oracle testbench (sort-based correctness check) ==="
	$(IVERILOG) -g2012 -o topk_heap_sim APEX_PKG.sv APEX_TOPK_HEAP.sv APEX_TOPK_HEAP_TB.sv
	$(VVP) topk_heap_sim

# Regenerate the trace-driven RTL cross-check output from an ACTUAL RTL run
# (not a hand-authored fixture): feeds xcheck_trace.txt through APEX_PIPELINE
# and writes measured per-descriptor status+latency to xcheck_rtl_out.txt.
xcheck:
	@echo "=== Trace-driven RTL cross-check (regenerates xcheck_rtl_out.txt) ==="
	$(IVERILOG) -g2012 -o apex_xcheck_sim $(RTL_CORE) APEX_XCHECK_TB.sv
	$(VVP) apex_xcheck_sim \
	    +TRACE=../experiments/_xcheck_out/xcheck_trace.txt \
	    +OUT=../experiments/_xcheck_out/xcheck_rtl_out.txt
	@echo "  Regenerated. Verify: python ../experiments/run_rtl_xcheck.py"

$(SIM_BIN): $(RTL_CORE) $(RTL_TB)
	$(IVERILOG) -g2012 -o $(SIM_BIN) $(RTL_CORE) $(RTL_TB)

# Compile-check CEFE modules (no testbench yet, syntax/elaboration only).
# APEX_PKG.sv is included because cefe_addr_mapper / cefe_dma_engine import
# apex_pkg::* for shared parameters.
cefe_check: $(RTL_CEFE) ICG.sv APEX_PKG.sv
	@echo "=== Checking CEFE modules (compile only) ==="
	$(IVERILOG) -g2012 -o cefe_check.out APEX_PKG.sv ICG.sv $(RTL_CEFE)
	@rm -f cefe_check.out
	@echo "  CEFE compile check PASSED"

synth:
	@echo "=== Launching synthesis (edit synth/synthesize_apex.tcl for your PDK) ==="
	cd synth && genus -files synthesize_apex.tcl

wave: $(VCD)
	$(GTKWAVE) $(VCD) &

clean:
	rm -f $(SIM_BIN) $(VCD)
	rm -rf synth/../results
