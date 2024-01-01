#=============================================================================
# Alveo U280 XDC Constraints — APEX Prototyping
#
# Target: xcu280-fsvh2892-2L-e
# System clock: 300 MHz differential (SLR0 HBM reference clock)
# Design clock: 250 MHz from MMCM (4.000 ns period)
# PCIe refclk: 100 MHz
#=============================================================================

#=============================================================================
# Primary Clock Definitions
#=============================================================================

# 300 MHz differential system clock (U280 SLR0 HBM reference clock)
# Pin locations from Alveo U280 schematic: SYSCLK0 (Bank 68)
set_property PACKAGE_PIN BJ44 [get_ports sys_clk_p]
set_property PACKAGE_PIN BJ45 [get_ports sys_clk_n]
set_property IOSTANDARD LVDS [get_ports sys_clk_p]
set_property IOSTANDARD LVDS [get_ports sys_clk_n]
set_property DIFF_TERM_ADV TERM_100 [get_ports sys_clk_p]

create_clock -period 3.333 -name sys_clk_300m [get_ports sys_clk_p]

# 250 MHz generated clock from MMCM output (APEX system clock)
# This is auto-derived by Vivado from the MMCM, but we constrain explicitly
# for clarity in timing reports.
create_generated_clock -name apex_clk \
    -source [get_pins u_clk_wiz/inst/mmcme4_adv_inst/CLKIN1] \
    -master_clock sys_clk_300m \
    [get_pins u_clk_wiz/inst/mmcme4_adv_inst/CLKOUT0]

# Explicit period constraint for the APEX domain (belt-and-suspenders)
create_clock -period 4.000 -name apex_clk_period \
    [get_pins u_clk_wiz/inst/mmcme4_adv_inst/CLKOUT0]

# PCIe reference clock (100 MHz, from PCIe edge connector)
# NOTE: In a full build with XDMA IP, the PCIe refclk is internal to the
# XDMA subsystem. These constraints are only needed if the refclk is
# explicitly brought to the top level for a custom PCIe configuration.
# U280 PCIe x16 slot refclk pins (uncomment if needed):
# set_property PACKAGE_PIN AR14 [get_ports pcie_refclk_p]
# set_property PACKAGE_PIN AR13 [get_ports pcie_refclk_n]
# create_clock -period 10.000 -name pcie_refclk [get_ports pcie_refclk_p]

#=============================================================================
# Reset Input
#=============================================================================
set_property PACKAGE_PIN BJ42 [get_ports sys_rst_n]
set_property IOSTANDARD LVCMOS18 [get_ports sys_rst_n]
set_property PULLUP true [get_ports sys_rst_n]

# Reset is asynchronous — mark as false path for source
set_false_path -from [get_ports sys_rst_n]

#=============================================================================
# Status LEDs (active-high, accent LEDs on U280 board)
set_property PACKAGE_PIN D32 [get_ports {led_status[0]}]
set_property PACKAGE_PIN D31 [get_ports {led_status[1]}]
set_property IOSTANDARD LVCMOS18 [get_ports {led_status[*]}]
set_property DRIVE 8 [get_ports {led_status[*]}]

#=============================================================================
# False Paths — Asynchronous Feedback Inputs
#
# The fb_* signals cross from GPU clock domain into the APEX pipeline.
# They are synchronized internally via the APEX_PIPELINE's feedback capture
# registers. Mark as false paths to prevent timing analysis across domains.
#=============================================================================
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *fb_chunk_id*}]
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *fb_attention_mass*}]
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *fb_expert_id*}]
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *fb_valid*}]

#=============================================================================
# False Paths — Quasi-Static Configuration Registers
#
# cfg_* signals are written via AXI-Lite and held stable during pipeline
# operation. They only change between decode steps when pipeline_idle=1.
# Mark as false paths since they are guaranteed stable during active scoring.
#=============================================================================
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *csr_reg[1]*}]
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *csr_reg[2]*}]
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *csr_reg[3]*}]
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *csr_reg[4]*}]

# Expert active mask (quasi-static)
set_false_path -from [get_cells -hierarchical -filter {NAME =~ *cfg_expert_active_mask*}]

#=============================================================================
# Max Delay — MAC Critical Path
#
# The MAC array (S4) is the timing-critical stage. The purely combinational
# path from pred_valid input through 7 multipliers + CSA tree to score_out
# register is the longest in the design. Constrain from the pipeline staging
# registers (s4_* in APEX_PIPELINE) to the MAC output register.
# Target: 3.2 ns (80% of 4.0 ns period) to provide timing margin.
#=============================================================================
set_max_delay 3.200 \
    -from [get_cells -hierarchical -filter {NAME =~ *u_apex_pipeline/u_mac/score_out_reg*}] \
    -to   [get_cells -hierarchical -filter {NAME =~ *u_apex_pipeline/u_heap/s5a_*_reg*}]

#=============================================================================
# Max Delay — Heap Critical Path (Top-K dual-zone)
#
# The heap sift operation (Case 1/2 cross-zone) is the second critical path.
# The combinational sift-down network reads ez_score registers and writes
# them back in the same cycle. Constrain register-to-register within the heap.
# Target: 3.5 ns to allow routing flexibility.
#=============================================================================
set_max_delay 3.500 \
    -from [get_cells -hierarchical -filter {NAME =~ *u_apex_pipeline/u_heap/ez_score_reg*}] \
    -to   [get_cells -hierarchical -filter {NAME =~ *u_apex_pipeline/u_heap/ez_score_reg*}]

#=============================================================================
# I/O Standards
#=============================================================================

# Differential clock already set to LVDS above

# AXI-Lite interface (directly from XDMA IP, internal — no physical pins)
# These constraints only apply if AXI-Lite is brought to physical I/O for debug.
# In normal operation, AXI-Lite is routed internally from XDMA BAR0.

#=============================================================================
# SLR Placement Constraints (Pblocks)
#
# U280 has 3 SLRs. Strategy:
#   SLR0: Clock infrastructure + AXI-Lite CSRs + PCIe/XDMA interface
#   SLR1: APEX Pipeline (scoring datapath — MAC, heap, weight update)
#   SLR2: CEFE front-end (vc_wrr, cfo_cam, bdb_parser) + HBM interface
#
# This placement minimizes SLR crossing on the hot path (WRR → Pipeline)
# by placing the WRR output register in SLR1's laguna region.
#=============================================================================

# SLR0: Clock + PCIe + CSR
create_pblock pblock_slr0_infra
add_cells_to_pblock [get_pblocks pblock_slr0_infra] [get_cells u_ibufds]
add_cells_to_pblock [get_pblocks pblock_slr0_infra] [get_cells u_clk_wiz]
resize_pblock [get_pblocks pblock_slr0_infra] -add {SLR0}

# SLR1: APEX Pipeline (performance-critical scoring path)
create_pblock pblock_slr1_apex
add_cells_to_pblock [get_pblocks pblock_slr1_apex] [get_cells u_apex_pipeline]
resize_pblock [get_pblocks pblock_slr1_apex] -add {SLR1}
set_property IS_SOFT FALSE [get_pblocks pblock_slr1_apex]

# SLR2: CEFE front-end
create_pblock pblock_slr2_cefe
add_cells_to_pblock [get_pblocks pblock_slr2_cefe] [get_cells u_bdb_parser]
add_cells_to_pblock [get_pblocks pblock_slr2_cefe] [get_cells u_cfo_cam]
add_cells_to_pblock [get_pblocks pblock_slr2_cefe] [get_cells u_vc_wrr]
resize_pblock [get_pblocks pblock_slr2_cefe] -add {SLR2}

#=============================================================================
# SLR Crossing — Pipeline Registers for Laguna
#
# The WRR output register (pop_valid_r, pop_data_r, pop_vc_id_r) sits at
# the SLR2→SLR1 boundary. Tag them for Laguna register insertion.
#=============================================================================
set_property USER_SLL_REG TRUE \
    [get_cells -hierarchical -filter {NAME =~ *u_vc_wrr/pop_valid_r_reg*}]
set_property USER_SLL_REG TRUE \
    [get_cells -hierarchical -filter {NAME =~ *u_vc_wrr/pop_data_r_reg*}]
set_property USER_SLL_REG TRUE \
    [get_cells -hierarchical -filter {NAME =~ *u_vc_wrr/pop_vc_id_r_reg*}]

#=============================================================================
# Bitstream Configuration Settings
#=============================================================================
set_property CONFIG_VOLTAGE 1.8 [current_design]
set_property CFGBVS GND [current_design]
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
set_property BITSTREAM.CONFIG.CONFIGRATE 85.0 [current_design]
set_property BITSTREAM.CONFIG.SPI_BUSWIDTH 4 [current_design]
set_property BITSTREAM.CONFIG.SPI_32BIT_ADDR YES [current_design]
set_property BITSTREAM.CONFIG.SPI_FALL_EDGE YES [current_design]
set_property BITSTREAM.CONFIG.OVERTEMPSHUTDOWN ENABLE [current_design]

# Enable bitstream-level CRC for integrity
set_property BITSTREAM.GENERAL.CRC ENABLE [current_design]

# Configuration from SPI flash (Alveo standard)
set_property CONFIG_MODE SPIx4 [current_design]

#=============================================================================
# Miscellaneous Timing Constraints
#=============================================================================

# Clock domain crossing between PCIe refclk and APEX clock
# Only relevant when XDMA IP is included in the build (uncomment if needed):
# set_clock_groups -asynchronous \
#     -group [get_clocks pcie_refclk] \
#     -group [get_clocks apex_clk]

# MMCM feedback path is internal — not a real clock crossing
set_false_path -from [get_clocks sys_clk_300m] -to [get_clocks apex_clk]

# Multicycle path for statistics counters (read by software, not timing-critical)
set_multicycle_path 2 -setup \
    -from [get_cells -hierarchical -filter {NAME =~ *u_apex_pipeline/stat_admitted_reg*}] \
    -to   [get_cells -hierarchical -filter {NAME =~ *csr_reg[5]*}]
set_multicycle_path 1 -hold \
    -from [get_cells -hierarchical -filter {NAME =~ *u_apex_pipeline/stat_admitted_reg*}] \
    -to   [get_cells -hierarchical -filter {NAME =~ *csr_reg[5]*}]

set_multicycle_path 2 -setup \
    -from [get_cells -hierarchical -filter {NAME =~ *u_apex_pipeline/stat_rejected_reg*}] \
    -to   [get_cells -hierarchical -filter {NAME =~ *csr_reg[6]*}]
set_multicycle_path 1 -hold \
    -from [get_cells -hierarchical -filter {NAME =~ *u_apex_pipeline/stat_rejected_reg*}] \
    -to   [get_cells -hierarchical -filter {NAME =~ *csr_reg[6]*}]
