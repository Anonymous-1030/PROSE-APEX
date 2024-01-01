#=============================================================================
# APEX Pipeline + CEFE Endpoint Design Constraints
# Technology: ASAP7 7nm FinFET (academic open PDK)
# Target: 1 GHz (1.0 ns period)
#
# Covers:
#   - APEX scoring pipeline (9-stage; scoring-pipeline area ~0.024 mm²
#     structural estimate, register-file expert banks)
#   - CEFE shared endpoint modules (VC-WRR, CFO CAM, BDB parser)
#   - Per-Expert-Bank clock gating constraints
#   - S6→S4 multicycle path (weight update off critical path)
#   - SZ min-tree idle-refresh multicycle path (safe_min primary tree)
#   - MAC unit critical path budget: mapped to a prefix adder via `+`, ~0.96 ns
#=============================================================================

#-----------------------------------------------------------------------------
# Clock Definition
#-----------------------------------------------------------------------------
create_clock -name clk -period 1.0 [get_ports clk]

set_clock_uncertainty -setup 0.05 [get_clocks clk]
set_clock_uncertainty -hold 0.02 [get_clocks clk]
set_clock_transition 0.03 [get_clocks clk]

#-----------------------------------------------------------------------------
# Per-Expert-Bank Independent Clock Gating
# Each of the 7 expert banks has its own ICG instance. Constrain the
# enable-to-gated-clock path independently to avoid cross-bank interference.
#-----------------------------------------------------------------------------
set expert_icg_cells [get_cells -hier -filter {ref_name =~ ICG && full_name =~ *gen_expert_banks*}]
foreach_in_collection icg $expert_icg_cells {
    set icg_name [get_attribute $icg full_name]
    # Enable setup: must be stable 0.1 ns before clock edge
    set_clock_gating_check -setup 0.10 -hold 0.05 $icg
}

#-----------------------------------------------------------------------------
# MAC Critical Path Budget Constraint (S4)
# The 7-wide multiply-accumulate tree is the functional critical path. The
# final CPA is left as `+` so synthesis maps it to a technology-optimal prefix
# adder (Kogge-Stone / Brent-Kung). Structural estimate is ~0.96 ns (see
# asic/reports/timing.rpt Path 1); budget it just under the period and let the
# tool pick the adder architecture that meets it.
#-----------------------------------------------------------------------------
set_max_delay 0.95 -from [get_pins -hier -filter {full_name =~ *u_mac/pred_*}] \
                    -to   [get_pins -hier -filter {full_name =~ *u_mac/score_out*}]

#-----------------------------------------------------------------------------
# Input Delays
# Command ring interface (from MMIO — relatively relaxed)
#-----------------------------------------------------------------------------
set_input_delay -clock clk -max 0.10 [get_ports cmd_chunk_id]
set_input_delay -clock clk -min 0.02 [get_ports cmd_chunk_id]
set_input_delay -clock clk -max 0.10 [get_ports cmd_epoch]
set_input_delay -clock clk -min 0.02 [get_ports cmd_epoch]
set_input_delay -clock clk -max 0.10 [get_ports cmd_namespace]
set_input_delay -clock clk -min 0.02 [get_ports cmd_namespace]
set_input_delay -clock clk -max 0.10 [get_ports cmd_priority]
set_input_delay -clock clk -min 0.02 [get_ports cmd_priority]
set_input_delay -clock clk -max 0.08 [get_ports cmd_valid]
set_input_delay -clock clk -min 0.02 [get_ports cmd_valid]

# Feedback interface (asynchronous, relaxed timing)
set_input_delay -clock clk -max 0.15 [get_ports fb_chunk_id]
set_input_delay -clock clk -min 0.02 [get_ports fb_chunk_id]
set_input_delay -clock clk -max 0.15 [get_ports fb_attention_mass]
set_input_delay -clock clk -min 0.02 [get_ports fb_attention_mass]
set_input_delay -clock clk -max 0.15 [get_ports fb_expert_id]
set_input_delay -clock clk -min 0.02 [get_ports fb_expert_id]
set_input_delay -clock clk -max 0.10 [get_ports fb_valid]
set_input_delay -clock clk -min 0.02 [get_ports fb_valid]

# Backpressure signals
set_input_delay -clock clk -max 0.08 [get_ports cpl_ready]
set_input_delay -clock clk -min 0.02 [get_ports cpl_ready]
set_input_delay -clock clk -max 0.08 [get_ports dma_ready]
set_input_delay -clock clk -min 0.02 [get_ports dma_ready]

# Residency management
set_input_delay -clock clk -max 0.10 [get_ports res_set_id]
set_input_delay -clock clk -min 0.02 [get_ports res_set_id]
set_input_delay -clock clk -max 0.08 [get_ports res_set_valid]
set_input_delay -clock clk -min 0.02 [get_ports res_set_valid]
set_input_delay -clock clk -max 0.10 [get_ports res_clear_id]
set_input_delay -clock clk -min 0.02 [get_ports res_clear_id]
set_input_delay -clock clk -max 0.08 [get_ports res_clear_valid]
set_input_delay -clock clk -min 0.02 [get_ports res_clear_valid]

#-----------------------------------------------------------------------------
# Output Delays
#-----------------------------------------------------------------------------
# Completion ring
set_output_delay -clock clk -max 0.10 [get_ports cpl_chunk_id]
set_output_delay -clock clk -min 0.02 [get_ports cpl_chunk_id]
set_output_delay -clock clk -max 0.08 [get_ports cpl_status]
set_output_delay -clock clk -min 0.02 [get_ports cpl_status]
set_output_delay -clock clk -max 0.08 [get_ports cpl_valid]
set_output_delay -clock clk -min 0.02 [get_ports cpl_valid]

# DMA issue
set_output_delay -clock clk -max 0.10 [get_ports dma_chunk_id]
set_output_delay -clock clk -min 0.02 [get_ports dma_chunk_id]
set_output_delay -clock clk -max 0.10 [get_ports dma_score]
set_output_delay -clock clk -min 0.02 [get_ports dma_score]
set_output_delay -clock clk -max 0.08 [get_ports dma_valid]
set_output_delay -clock clk -min 0.02 [get_ports dma_valid]

# Command ready
set_output_delay -clock clk -max 0.08 [get_ports cmd_ready]
set_output_delay -clock clk -min 0.02 [get_ports cmd_ready]

# Statistics (non-critical)
set_output_delay -clock clk -max 0.20 [get_ports stat_admitted]
set_output_delay -clock clk -max 0.20 [get_ports stat_rejected]
set_output_delay -clock clk -max 0.20 [get_ports stat_total_cycles]

#-----------------------------------------------------------------------------
# Load Capacitance (fF)
#-----------------------------------------------------------------------------
set_load 4.0 [get_ports cpl_chunk_id]
set_load 3.0 [get_ports cpl_status]
set_load 2.0 [get_ports cpl_valid]
set_load 4.0 [get_ports dma_chunk_id]
set_load 4.0 [get_ports dma_score]
set_load 2.0 [get_ports dma_valid]
set_load 2.0 [get_ports cmd_ready]
set_load 3.0 [get_ports stat_*]

#-----------------------------------------------------------------------------
# Power Constraints
#-----------------------------------------------------------------------------
set_max_dynamic_power 12e-3   ;# 12 mW
set_max_leakage_power 4e-3    ;# 4 mW
set_max_total_power 16e-3     ;# 16 mW

#-----------------------------------------------------------------------------
# Area Constraint
#-----------------------------------------------------------------------------
set_max_area 100000           ;# 0.10 mm² = 100,000 µm²

#-----------------------------------------------------------------------------
# Operating Conditions
#-----------------------------------------------------------------------------
set_operating_conditions -analysis_type bc_wc

#-----------------------------------------------------------------------------
# Wire Load Model (ASAP7)
#-----------------------------------------------------------------------------
# Note: ASAP7 uses "Zero" wireload by default in academic flows.
# For actual tapeout, replace with extracted parasitics from P&R.
set_wire_load_model -name "Zero" -library asap7sc7p5t_SEQ_RVT_TT

#-----------------------------------------------------------------------------
# Clock Gating
#-----------------------------------------------------------------------------
set_clock_gating_style -max_fanout 32 -positive_edge_logic {latch}
set_clock_gating_check -setup 0.2 -hold 0.1

#-----------------------------------------------------------------------------
# Multicycle Paths
#-----------------------------------------------------------------------------
# S6 (Weight update) to S4 (MAC array): weights change once per decode step
# (~1000 cycles), not per descriptor. This is the critical multicycle path
# described in the paper — 2-cycle setup relaxation.
set_multicycle_path -setup 2 -from [get_cells u_weights/*] \
                              -to [get_cells u_mac/*weight*]
set_multicycle_path -hold 1 -from [get_cells u_weights/*] \
                             -to [get_cells u_mac/*weight*]

# Equivalent constraint using hierarchical pin filtering (per paper §5.4):
# S6 pipeline stage registers → S4 MAC weight inputs
set_multicycle_path 2 -setup \
    -from [get_pins -hier -filter {full_name =~ *u_weights*/*}] \
    -to   [get_pins -hier -filter {full_name =~ *u_mac*/*}]
set_multicycle_path 1 -hold \
    -from [get_pins -hier -filter {full_name =~ *u_weights*/*}] \
    -to   [get_pins -hier -filter {full_name =~ *u_mac*/*}]

# Top-K heap sift-down spans 2 pipeline stages (S5a + S5b)
# The heap_next combinational logic feeds into the S5b register
# No multicycle needed — it's within a single cycle (S5b)

#-----------------------------------------------------------------------------
# SZ min-tree (safe_min) multicycle — PRIMARY (idle-refresh) tree only.
# The primary Safe-Zone min-tree (sz_min_comb -> safe_min_reg) is a ~1650 ps
# 5-level comparator chain (asic/reports/timing.rpt Path 3). It is refreshed
# only on idle cycles (no admission in flight), so it is a legitimate 2-cycle
# multicycle path.
#-----------------------------------------------------------------------------
set_multicycle_path 2 -setup \
    -from [get_cells u_heap/sz_score*] \
    -to   [get_cells u_heap/safe_min*]
set_multicycle_path 1 -hold \
    -from [get_cells u_heap/sz_score*] \
    -to   [get_cells u_heap/safe_min*]

# WARNING (not resolved by this SDC): the SPECULATIVE forwarding min-tree
# (sz_min_fwd -> safe_min_reg) is used on the Case 2 admission cycle itself and
# therefore CANNOT be declared multicycle — doing so would be a false timing
# exception. At ~1650 ps it does not close at 1 GHz as-is. Real signoff
# requires pipelining the SZ tree (split at level 3) or lowering Fmax for this
# block. This constraint intentionally does NOT cover the forwarding path so
# STA will (correctly) report it as failing until the RTL is pipelined.
# set_multicycle_path on sz_min_fwd is deliberately OMITTED.

#-----------------------------------------------------------------------------
# False Paths
#-----------------------------------------------------------------------------
set_false_path -from [get_ports rst_n]
set_false_path -to [get_ports stat_*]
set_false_path -from [get_ports cfg_current_epoch]
set_false_path -from [get_ports cfg_current_namespace]
set_false_path -from [get_ports cfg_eta_q]
set_false_path -from [get_ports cfg_flush]

#-----------------------------------------------------------------------------
# Design Rule Constraints
#-----------------------------------------------------------------------------
set_max_fanout 24 [get_designs APEX_PIPELINE]
set_max_transition 0.06 [get_designs APEX_PIPELINE]
set_max_capacitance 0.12 [get_designs APEX_PIPELINE]

#-----------------------------------------------------------------------------
# CEFE Endpoint Modules Constraints (when synthesized as part of full endpoint)
# These apply only if cefe_vc_wrr / cefe_cfo_cam / cefe_bdb_parser are
# included in the synthesis scope.
#-----------------------------------------------------------------------------
# VC-WRR: deficit counter update must complete within one cycle
# CAM: parallel 16-entry match must complete within 0.80 ns
# BDB parser: DMA burst state machine is relaxed (not on critical path)

# Uncomment when synthesizing full CEFE endpoint:
# set_max_delay 0.80 -from [get_pins -hier -filter {full_name =~ *cefe_cfo_cam*match*}] \
#                     -to   [get_pins -hier -filter {full_name =~ *cefe_cfo_cam*match_idx*}]
# set_max_delay 0.90 -from [get_pins -hier -filter {full_name =~ *cefe_vc_wrr*deficit*}] \
#                     -to   [get_pins -hier -filter {full_name =~ *cefe_vc_wrr*grant*}]
