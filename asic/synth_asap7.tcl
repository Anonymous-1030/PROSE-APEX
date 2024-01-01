##############################################################################
## Synopsys Design Compiler Synthesis Script
## Target: ASAP7 7nm FinFET (asap7sc7p5t RVT, TT corner)
## Design: APEX_PIPELINE
## Author: PROSE-APEX Team
## Date:   2025-01-15
##############################################################################

#-----------------------------------------------------------------------------
# Setup
#-----------------------------------------------------------------------------
set TOP_MODULE   APEX_PIPELINE
set RTL_PATH     ../rtl
set RPT_PATH     ./reports
set RESULT_PATH  ./results

file mkdir $RPT_PATH
file mkdir $RESULT_PATH

#-----------------------------------------------------------------------------
# Library Settings - ASAP7 7nm
#-----------------------------------------------------------------------------
# ASAP7 install root; override via the ASAP7_DIR environment variable.
if {[info exists ::env(ASAP7_DIR)]} {
    set ASAP7_DIR $::env(ASAP7_DIR)
} else {
    set ASAP7_DIR /tools/pdk/asap7/asap7sc7p5t_27
}
set LIB_CORNER   TT
set LIB_VT       RVT

set_app_var target_library  "${ASAP7_DIR}/LIB/NLDM/asap7sc7p5t_SEQ_${LIB_VT}_${LIB_CORNER}_nldm_220122.db \
                             ${ASAP7_DIR}/LIB/NLDM/asap7sc7p5t_OA_${LIB_VT}_${LIB_CORNER}_nldm_220122.db \
                             ${ASAP7_DIR}/LIB/NLDM/asap7sc7p5t_SIMPLE_${LIB_VT}_${LIB_CORNER}_nldm_220122.db \
                             ${ASAP7_DIR}/LIB/NLDM/asap7sc7p5t_AO_${LIB_VT}_${LIB_CORNER}_nldm_220122.db \
                             ${ASAP7_DIR}/LIB/NLDM/asap7sc7p5t_INVBUF_${LIB_VT}_${LIB_CORNER}_nldm_220122.db"

set_app_var link_library    "* $target_library"
set_app_var symbol_library  {}

set_app_var search_path     [list $ASAP7_DIR/LIB/NLDM ${RTL_PATH}]

#-----------------------------------------------------------------------------
# Read RTL Design Files
#-----------------------------------------------------------------------------
analyze -format sverilog [list \
    ${RTL_PATH}/APEX_PKG.sv \
    ${RTL_PATH}/APEX_MAC_ARRAY.sv \
    ${RTL_PATH}/APEX_TOPK_HEAP.sv \
    ${RTL_PATH}/APEX_EXPERT_BANK.sv \
    ${RTL_PATH}/APEX_PCM.sv \
    ${RTL_PATH}/APEX_WEIGHT_UPDATE.sv \
    ${RTL_PATH}/APEX_PIPELINE_CTRL.sv \
    ${RTL_PATH}/APEX_LOSS_COMPUTE.sv \
    ${RTL_PATH}/APEX_SEA.sv \
    ${RTL_PATH}/APEX_PIPELINE.sv \
]

elaborate $TOP_MODULE
current_design $TOP_MODULE
link

#-----------------------------------------------------------------------------
# Clock Definition - 1 GHz (1.0 ns period)
#-----------------------------------------------------------------------------
create_clock -name clk -period 1.0 [get_ports clk]
set_clock_uncertainty 0.05 [get_clocks clk]
set_clock_transition  0.03 [get_clocks clk]

set_input_delay  0.08 -clock clk [remove_from_collection [all_inputs] [get_ports clk]]
set_output_delay 0.08 -clock clk [all_outputs]

set_driving_cell -lib_cell INVx1_ASAP7_75t_R -pin Y [all_inputs]
set_load 0.5 [all_outputs]

#-----------------------------------------------------------------------------
# Timing Constraints - MAC Path
#-----------------------------------------------------------------------------
set_max_delay 0.60 -from [get_pins u_mac_array/pred_in_reg*/Q] \
                    -to   [get_pins u_mac_array/score_out_reg*/D]

#-----------------------------------------------------------------------------
# Timing Constraints - Dual-Zone Top-K Forwarding Path
# The speculative safe_min forwarding mux (17-comparator min-tree) must
# settle within the 0.45 ns critical-path budget of the sift-down stage.
# This constrains the path from safe_min_idx register through the forwarding
# comparator network to the safe_min register input.
#-----------------------------------------------------------------------------
set_max_delay 0.45 -from [get_pins -hierarchical -filter {NAME =~ *u_heap*safe_min_idx_reg*/Q}] \
                   -to   [get_pins -hierarchical -filter {NAME =~ *u_heap*safe_min_reg*/D}]

set_max_delay 0.45 -from [get_pins -hierarchical -filter {NAME =~ *u_heap*sz_score_reg*/Q}] \
                   -to   [get_pins -hierarchical -filter {NAME =~ *u_heap*safe_min_reg*/D}]

#-----------------------------------------------------------------------------
# Area Constraint
#-----------------------------------------------------------------------------
set_max_area 100000

#-----------------------------------------------------------------------------
# Multicycle Path - Weight Update to MAC
# Weight update operates at half rate; MAC reads stable weights
#-----------------------------------------------------------------------------
set_multicycle_path 2 -setup \
    -from [get_cells u_weight_update/w_reg*] \
    -to   [get_cells u_mac_array/w_in_reg*]

set_multicycle_path 1 -hold \
    -from [get_cells u_weight_update/w_reg*] \
    -to   [get_cells u_mac_array/w_in_reg*]

#-----------------------------------------------------------------------------
# Power Optimization
#-----------------------------------------------------------------------------
set_clock_gating_style -sequential_cell latch \
                       -control_point before \
                       -control_signal scan_enable

insert_clock_gating -global

#-----------------------------------------------------------------------------
# Compile
#-----------------------------------------------------------------------------
compile_ultra -gate_clock -retime -no_autoungroup

#-----------------------------------------------------------------------------
# Reports
#-----------------------------------------------------------------------------
report_area -hierarchy           > ${RPT_PATH}/area.rpt
report_timing -max_paths 10      > ${RPT_PATH}/timing.rpt
report_power -hierarchy          > ${RPT_PATH}/power.rpt
report_constraint -all_violators > ${RPT_PATH}/constraint.rpt
report_qor                       > ${RPT_PATH}/qor.rpt

#-----------------------------------------------------------------------------
# Save Results
#-----------------------------------------------------------------------------
change_names -rules verilog -hierarchy
write -format verilog -hierarchy -output ${RESULT_PATH}/${TOP_MODULE}_synth.v
write -format ddc     -hierarchy -output ${RESULT_PATH}/${TOP_MODULE}_synth.ddc
write_sdc                                ${RESULT_PATH}/${TOP_MODULE}_synth.sdc
write_sdf                                ${RESULT_PATH}/${TOP_MODULE}_synth.sdf

puts "================================================================"
puts " Synthesis Complete: ${TOP_MODULE}"
puts " Target: ASAP7 7nm (${LIB_VT}, ${LIB_CORNER} corner)"
puts " Clock:  1.0 GHz"
puts "================================================================"

exit
