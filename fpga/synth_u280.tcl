#=============================================================================
# Vivado Batch-Mode Synthesis & Implementation Script
# Target: Alveo U280 (xcu280-fsvh2892-2L-e)
# Design: APEX Pipeline + CEFE Front-End Prototyping
#
# Usage:
#   vivado -mode batch -source synth_u280.tcl
#   vivado -mode batch -source synth_u280.tcl -tclargs -jobs 8
#
# Output: ./u280_apex_build/ (project + reports + bitstream)
#=============================================================================

#-----------------------------------------------------------------------------
# Parse arguments
#-----------------------------------------------------------------------------
set num_jobs 4
if {[llength $argv] > 0} {
    for {set i 0} {$i < [llength $argv]} {incr i} {
        if {[lindex $argv $i] eq "-jobs"} {
            incr i
            set num_jobs [lindex $argv $i]
        }
    }
}

puts "INFO: Running with $num_jobs parallel jobs"

#-----------------------------------------------------------------------------
# Configuration
#-----------------------------------------------------------------------------
set project_name    "u280_apex"
set project_dir     "./u280_apex_build"
set part            "xcu280-fsvh2892-2L-e"
set top_module      "u280_apex_top"
set target_freq_mhz 250

# Source directories (relative to script location)
set script_dir [file dirname [file normalize [info script]]]
set rtl_dir    [file normalize "$script_dir/../rtl"]
set fpga_dir   [file normalize "$script_dir"]

#-----------------------------------------------------------------------------
# Create Project (Non-Project / Out-of-Context mode)
#-----------------------------------------------------------------------------
puts "INFO: Creating project '$project_name' for part $part"

# Remove previous build if it exists
if {[file exists $project_dir]} {
    file delete -force $project_dir
}

create_project $project_name $project_dir -part $part -force
set_property target_language SystemVerilog [current_project]
set_property simulator_language Mixed [current_project]

# Set board part for Alveo U280 (enables board-aware IP configuration)
set_property board_part xilinx.com:au280:part0:1.2 [current_project]

#-----------------------------------------------------------------------------
# Add RTL Source Files
#-----------------------------------------------------------------------------
puts "INFO: Adding RTL sources from $rtl_dir"

# Add all SystemVerilog RTL files from the rtl directory
set rtl_files [glob -directory $rtl_dir *.sv]

# Exclude testbench files from synthesis
set synth_files {}
foreach f $rtl_files {
    if {![string match "*_TB*" $f] && ![string match "*_tb*" $f]} {
        lappend synth_files $f
    }
}

add_files -fileset sources_1 $synth_files
puts "INFO: Added [llength $synth_files] RTL source files"

# Add the FPGA top-level wrapper
add_files -fileset sources_1 [file normalize "$fpga_dir/u280_top.sv"]
puts "INFO: Added FPGA wrapper: u280_top.sv"

# Set top module
set_property top $top_module [current_fileset]

#-----------------------------------------------------------------------------
# Add Constraints
#-----------------------------------------------------------------------------
puts "INFO: Adding XDC constraints"

add_files -fileset constrs_1 [file normalize "$fpga_dir/u280_constraints.xdc"]
set_property used_in_synthesis true \
    [get_files [file normalize "$fpga_dir/u280_constraints.xdc"]]
set_property used_in_implementation true \
    [get_files [file normalize "$fpga_dir/u280_constraints.xdc"]]

#-----------------------------------------------------------------------------
# Synthesis Settings
#-----------------------------------------------------------------------------
puts "INFO: Configuring synthesis strategy"

set_property strategy Flow_PerfOptimized_high [get_runs synth_1]

# Key synthesis directives
set_property -name {STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS} -value {
    -retiming
    -no_srlextract
} -objects [get_runs synth_1]

set_property STEPS.SYNTH_DESIGN.ARGS.FLATTEN_HIERARCHY rebuilt [get_runs synth_1]
set_property STEPS.SYNTH_DESIGN.ARGS.KEEP_EQUIVALENT_REGISTERS true [get_runs synth_1]
set_property STEPS.SYNTH_DESIGN.ARGS.FSM_EXTRACTION one_hot [get_runs synth_1]
set_property STEPS.SYNTH_DESIGN.ARGS.RESOURCE_SHARING auto [get_runs synth_1]
set_property STEPS.SYNTH_DESIGN.ARGS.SHREG_MIN_SIZE 5 [get_runs synth_1]
set_property STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM_CASCADE_HEIGHT 4 [get_runs synth_1]

# Define SYNTHESIS macro for conditional compilation
set_property verilog_define {SYNTHESIS=1} [current_fileset]

#-----------------------------------------------------------------------------
# Run Synthesis
#-----------------------------------------------------------------------------
puts "INFO: Launching synthesis..."

launch_runs synth_1 -jobs $num_jobs
wait_on_run synth_1

if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    puts "ERROR: Synthesis failed!"
    exit 1
}

puts "INFO: Synthesis complete."

# Open synthesized design for reports
open_run synth_1 -name synth_1

#-----------------------------------------------------------------------------
# Post-Synthesis Reports
#-----------------------------------------------------------------------------
set rpt_dir "$project_dir/reports"
file mkdir $rpt_dir

report_utilization -file "$rpt_dir/post_synth_utilization.rpt"
report_timing_summary -max_paths 20 -file "$rpt_dir/post_synth_timing.rpt"
report_clocks -file "$rpt_dir/post_synth_clocks.rpt"
report_clock_interaction -file "$rpt_dir/post_synth_clock_interaction.rpt"

puts "INFO: Post-synthesis reports written to $rpt_dir"

#-----------------------------------------------------------------------------
# Implementation Settings
#-----------------------------------------------------------------------------
puts "INFO: Configuring implementation strategy"

set_property strategy Performance_ExtraTimingOpt [get_runs impl_1]

# opt_design: aggressive optimization for UltraScale+
set_property STEPS.OPT_DESIGN.ARGS.DIRECTIVE ExploreWithRemap [get_runs impl_1]

# place_design: SSI-aware placement for multi-SLR
set_property STEPS.PLACE_DESIGN.ARGS.DIRECTIVE SSI_SpreadLogic_high [get_runs impl_1]

# Physical optimization pass (post-place)
set_property STEPS.PHYS_OPT_DESIGN.IS_ENABLED true [get_runs impl_1]
set_property STEPS.PHYS_OPT_DESIGN.ARGS.DIRECTIVE AggressiveExplore [get_runs impl_1]

# route_design: timing-driven with high effort
set_property STEPS.ROUTE_DESIGN.ARGS.DIRECTIVE AggressiveExplore [get_runs impl_1]

# Post-route physical optimization
set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED true [get_runs impl_1]
set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.ARGS.DIRECTIVE AggressiveExplore [get_runs impl_1]

#-----------------------------------------------------------------------------
# Run Implementation
#-----------------------------------------------------------------------------
puts "INFO: Launching implementation..."

launch_runs impl_1 -jobs $num_jobs
wait_on_run impl_1

if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    puts "ERROR: Implementation failed!"
    exit 2
}

puts "INFO: Implementation complete."

# Open implemented design for reports
open_run impl_1 -name impl_1

#-----------------------------------------------------------------------------
# Post-Implementation Reports
#-----------------------------------------------------------------------------
report_utilization -file "$rpt_dir/post_impl_utilization.rpt" -hierarchical
report_timing_summary -max_paths 50 -file "$rpt_dir/post_impl_timing.rpt"
report_timing -from [get_cells -hierarchical -filter {NAME =~ *u_mac*}] \
    -to [get_cells -hierarchical -filter {NAME =~ *u_mac*}] \
    -max_paths 10 -file "$rpt_dir/post_impl_mac_timing.rpt"
report_power -file "$rpt_dir/post_impl_power.rpt"
report_clock_utilization -file "$rpt_dir/post_impl_clock_util.rpt"
report_route_status -file "$rpt_dir/post_impl_route_status.rpt"
report_drc -file "$rpt_dir/post_impl_drc.rpt"
report_methodology -file "$rpt_dir/post_impl_methodology.rpt"

# SLR utilization report (critical for multi-SLR timing)
report_utilization -slr -file "$rpt_dir/post_impl_slr_utilization.rpt"

puts "INFO: Post-implementation reports written to $rpt_dir"

#-----------------------------------------------------------------------------
# Check Timing — STRICT enforcement: abort on ANY setup violation
#-----------------------------------------------------------------------------
set wns [get_property STATS.WNS [get_runs impl_1]]
set tns [get_property STATS.TNS [get_runs impl_1]]
set whs [get_property STATS.WHS [get_runs impl_1]]

puts "INFO: Timing Summary:"
puts "  WNS = ${wns} ns"
puts "  TNS = ${tns} ns"
puts "  WHS = ${whs} ns"

if {$wns < 0} {
    puts "CRITICAL ERROR: Setup timing violation (WNS = ${wns} ns)."
    puts "  Design does NOT meet 250 MHz target. Aborting bitstream generation."
    puts "  Action: Review post_impl_timing.rpt and constrain critical paths."
    exit 3
}

if {$whs < 0} {
    puts "CRITICAL ERROR: Hold timing violation (WHS = ${whs} ns)."
    puts "  Bitstream will NOT be generated — hold violations cannot be tolerated."
    exit 4
}

puts "INFO: Timing PASSED. WNS = ${wns} ns (positive = margin)."

#-----------------------------------------------------------------------------
# Generate Bitstream
#-----------------------------------------------------------------------------
puts "INFO: Generating bitstream..."

launch_runs impl_1 -to_step write_bitstream -jobs $num_jobs
wait_on_run impl_1

# Copy bitstream to a convenient location
set bit_file [glob -nocomplain "$project_dir/$project_name.runs/impl_1/*.bit"]
if {[llength $bit_file] > 0} {
    file copy -force [lindex $bit_file 0] "$project_dir/${project_name}.bit"
    puts "INFO: Bitstream: $project_dir/${project_name}.bit"
} else {
    puts "WARNING: Bitstream file not found in expected location"
}

#-----------------------------------------------------------------------------
# Generate Memory Configuration File (for flash programming)
#-----------------------------------------------------------------------------
if {[llength $bit_file] > 0} {
    write_cfgmem -format mcs -size 128 -interface SPIx4 \
        -loadbit "up 0x01002000 [lindex $bit_file 0]" \
        -file "$project_dir/${project_name}.mcs" -force
    puts "INFO: MCS file: $project_dir/${project_name}.mcs"
}

#-----------------------------------------------------------------------------
# Summary
#-----------------------------------------------------------------------------
puts ""
puts "=========================================="
puts " APEX U280 Build Complete"
puts "=========================================="
puts " Part:       $part"
puts " Target:     ${target_freq_mhz} MHz (4.000 ns)"
puts " WNS:        ${wns} ns"
puts " TNS:        ${tns} ns"
puts " WHS:        ${whs} ns"
puts " Bitstream:  $project_dir/${project_name}.bit"
puts " Reports:    $rpt_dir/"
puts "=========================================="
puts ""

exit 0
