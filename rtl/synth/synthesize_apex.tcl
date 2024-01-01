#!/bin/tclsh
#=============================================================================
# APEX Pipeline Synthesis Script
# Tool: Cadence Genus (primary) / Synopsys Design Compiler (fallback)
# Target: ASAP7 7nm FinFET (academic open PDK)
# Clock: 1GHz (1ns period)
#
# Usage (Genus):
#   genus -files synthesize_apex.tcl
#
# Usage (DC):
#   dc_shell -f synthesize_apex.tcl
#=============================================================================

#-----------------------------------------------------------------------------
# Tool Detection
#-----------------------------------------------------------------------------
if {[info exists ::env(GENUS_HOME)] || [string match "*genus*" [info nameofexecutable]]} {
    set TOOL "genus"
    puts "INFO: Detected Cadence Genus"
} else {
    set TOOL "dc"
    puts "INFO: Assuming Synopsys Design Compiler"
}

#-----------------------------------------------------------------------------
# Setup
#-----------------------------------------------------------------------------
set TOP_MODULE "APEX_PIPELINE"
# Run from rtl/synth/; the SystemVerilog sources live one level up in rtl/.
set RTL_DIR ".."
set RESULT_DIR "./results/apex"
set REPORT_DIR "./results/apex/reports"

file mkdir ${RESULT_DIR}
file mkdir ${REPORT_DIR}

#-----------------------------------------------------------------------------
# Library Setup
# ASAP7 7nm academic PDK (primary target)
# Adjust paths for your PDK installation
#-----------------------------------------------------------------------------
if {$TOOL == "genus"} {
    # Cadence Genus library setup — ASAP7 7nm
    set_db init_lib_search_path {
        /path/to/asap7/lib/timing
        /path/to/asap7/lib/lef
        /path/to/asap7/qrc
    }
    set_db library {
        asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib
        asap7sc7p5t_OA_RVT_TT_nldm_220122.lib
        asap7sc7p5t_AO_RVT_TT_nldm_220122.lib
        asap7sc7p5t_SEQ_RVT_TT_nldm_220122.lib
        asap7sc7p5t_SIMPLE_RVT_TT_nldm_220122.lib
    }
    set_db lef_library {
        asap7_tech_1x_201209.lef
        asap7sc7p5t_28_R_1x_220121a.lef
    }
} else {
    # Synopsys DC library setup — ASAP7 7nm
    set ASAP7_PATH "/path/to/asap7"
    set TARGET_LIB [list \
        ${ASAP7_PATH}/lib/asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.db \
        ${ASAP7_PATH}/lib/asap7sc7p5t_OA_RVT_TT_nldm_220122.db \
        ${ASAP7_PATH}/lib/asap7sc7p5t_AO_RVT_TT_nldm_220122.db \
        ${ASAP7_PATH}/lib/asap7sc7p5t_SEQ_RVT_TT_nldm_220122.db \
        ${ASAP7_PATH}/lib/asap7sc7p5t_SIMPLE_RVT_TT_nldm_220122.db \
    ]
    set LINK_LIB "* $TARGET_LIB"
    set_app_var target_library $TARGET_LIB
    set_app_var link_library $LINK_LIB
    set_app_var search_path "${ASAP7_PATH}/lib ${ASAP7_PATH}/lef"
}

#-----------------------------------------------------------------------------
# Read Design
#-----------------------------------------------------------------------------
set RTL_FILES [list \
    ${RTL_DIR}/ICG.sv \
    ${RTL_DIR}/APEX_EXPERT_BANK.sv \
    ${RTL_DIR}/APEX_PCM.sv \
    ${RTL_DIR}/APEX_MAC_ARRAY.sv \
    ${RTL_DIR}/APEX_TOPK_HEAP.sv \
    ${RTL_DIR}/APEX_WEIGHT_UPDATE.sv \
    ${RTL_DIR}/APEX_PIPELINE_CTRL.sv \
    ${RTL_DIR}/APEX_PIPELINE.sv \
    ${RTL_DIR}/cefe_vc_wrr.sv \
    ${RTL_DIR}/cefe_cfo_cam.sv \
    ${RTL_DIR}/cefe_bdb_parser.sv \
]

if {$TOOL == "genus"} {
    foreach f $RTL_FILES {
        read_hdl -sv $f
    }
    elaborate ${TOP_MODULE}
    check_design -unresolved
} else {
    foreach f $RTL_FILES {
        analyze -format sverilog $f
    }
    elaborate ${TOP_MODULE}
    current_design ${TOP_MODULE}
    link
    check_design
}

#-----------------------------------------------------------------------------
# Constraints
#-----------------------------------------------------------------------------
if {$TOOL == "genus"} {
    read_sdc APEX_PIPELINE.sdc
} else {
    source APEX_PIPELINE.sdc
}

#-----------------------------------------------------------------------------
# Synthesis
#-----------------------------------------------------------------------------
if {$TOOL == "genus"} {
    # Genus synthesis flow
    set_db syn_generic_effort high
    set_db syn_map_effort high
    set_db syn_opt_effort high

    syn_generic
    syn_map
    syn_opt

    # Additional optimization passes
    syn_opt -incremental
} else {
    # DC synthesis flow — preserve hierarchy for review (no_autoungroup)
    compile_ultra -no_autoungroup -area_high_effort_script
    compile_ultra -no_autoungroup -gate_clock -retime -incremental
}

#-----------------------------------------------------------------------------
# Reports
#-----------------------------------------------------------------------------
if {$TOOL == "genus"} {
    report_area > ${REPORT_DIR}/area.rpt
    report_area -detail > ${REPORT_DIR}/area_detail.rpt
    report_power > ${REPORT_DIR}/power.rpt
    report_power -detail > ${REPORT_DIR}/power_detail.rpt
    report_timing -nworst 10 > ${REPORT_DIR}/timing.rpt
    report_timing -lint > ${REPORT_DIR}/timing_lint.rpt
    report_gates > ${REPORT_DIR}/gates.rpt
    report_qor > ${REPORT_DIR}/qor.rpt
    report_dp > ${REPORT_DIR}/datapath.rpt
    report_clock_gating > ${REPORT_DIR}/clock_gating.rpt
    report_messages -all > ${REPORT_DIR}/messages.rpt

    # Critical path detail
    report_timing -from [get_pins u_mac/*] -to [get_pins u_mac/score_out*] \
        -nworst 5 > ${REPORT_DIR}/timing_mac_path.rpt
    report_timing -from [get_pins u_heap/*] -to [get_pins u_heap/heap*] \
        -nworst 5 > ${REPORT_DIR}/timing_heap_path.rpt
} else {
    report_area -hierarchy > ${REPORT_DIR}/area.rpt
    report_area -physical > ${REPORT_DIR}/area_physical.rpt
    report_power -analysis_effort high > ${REPORT_DIR}/power.rpt
    report_power -hierarchy > ${REPORT_DIR}/power_hier.rpt
    report_timing -nworst 10 -max_paths 10 > ${REPORT_DIR}/timing.rpt
    report_timing -path_type full -delay_type max > ${REPORT_DIR}/timing_max.rpt
    report_timing -path_type full -delay_type min > ${REPORT_DIR}/timing_min.rpt
    report_cell > ${REPORT_DIR}/cells.rpt
    report_design > ${REPORT_DIR}/design.rpt
    report_qor > ${REPORT_DIR}/qor.rpt
    report_clock_gating > ${REPORT_DIR}/clock_gating.rpt
    report_constraints -all_violators > ${REPORT_DIR}/violations.rpt
}

#-----------------------------------------------------------------------------
# Output Netlists
#-----------------------------------------------------------------------------
if {$TOOL == "genus"} {
    write_hdl > ${RESULT_DIR}/${TOP_MODULE}_netlist.v
    write_sdc > ${RESULT_DIR}/${TOP_MODULE}_out.sdc
    write_sdf > ${RESULT_DIR}/${TOP_MODULE}.sdf
    write_design -innovus -basename ${RESULT_DIR}/${TOP_MODULE}
} else {
    write -format ddc -hierarchy -output ${RESULT_DIR}/${TOP_MODULE}.ddc
    write -format verilog -hierarchy -output ${RESULT_DIR}/${TOP_MODULE}_netlist.v
    write_sdf ${RESULT_DIR}/${TOP_MODULE}.sdf
    write_sdc ${RESULT_DIR}/${TOP_MODULE}_out.sdc
}

#-----------------------------------------------------------------------------
# Summary
#-----------------------------------------------------------------------------
puts "==================================================================="
puts "Synthesis Complete for ${TOP_MODULE}"
puts "Tool: $TOOL"
puts "Target: ASAP7 7nm @ 1GHz"
puts "==================================================================="
puts ""
puts "Key results:"
puts "  Area report:   ${REPORT_DIR}/area.rpt"
puts "  Timing report: ${REPORT_DIR}/timing.rpt"
puts "  Power report:  ${REPORT_DIR}/power.rpt"
puts ""
puts "Check timing report for:"
puts "  - MAC critical path (S4): target < 1.0ns"
puts "  - Heap sift-down (S5b): target < 1.0ns"
puts "  - Reject bypass (S2b→cpl): target < 1.0ns"
puts "==================================================================="

# Print timing summary inline
if {$TOOL == "genus"} {
    report_timing -nworst 3
} else {
    report_timing -nworst 3
}

puts "==================================================================="
