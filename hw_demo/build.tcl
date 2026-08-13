# Quartus project + compile for the scoped-down single-channel demo, sized to
# actually fit an EP4CE6E22C8N (6,272 LEs). Reuses the same verified
# rtl/rrc_fir_core.v, booth_mult_r4.v, pipelined_adder_tree.v, rrc_coeff_rom.v
# as the main design; only the top level and tap count are different.
#
# Run from the repository root:
#     quartus_sh -t hw_demo/build.tcl
#     quartus_sta -t hw_demo/report_timing.tcl

package require ::quartus::project
package require ::quartus::flow

set project_name "rrc_fir_single_ep4ce6"
set top_level    "top_single_channel"
set device       "EP4CE6E22C8"
set family       "Cyclone IV E"
set out_dir      "hw_demo/output"

for {set i 0} {$i < [llength $quartus(args)]} {incr i} {
    set arg [lindex $quartus(args) $i]
    switch -- $arg {
        -device  { incr i; set device  [lindex $quartus(args) $i] }
        -family  { incr i; set family  [lindex $quartus(args) $i] }
        -out     { incr i; set out_dir [lindex $quartus(args) $i] }
    }
}

file mkdir $out_dir

if {[project_exists $project_name]} {
    project_open $project_name -revision $project_name
} else {
    project_new $project_name -revision $project_name -overwrite
}

set_global_assignment -name FAMILY $family
set_global_assignment -name DEVICE $device
set_global_assignment -name TOP_LEVEL_ENTITY $top_level
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY $out_dir
set_global_assignment -name VERILOG_INPUT_VERSION VERILOG_2001

set_global_assignment -name SEARCH_PATH "hw_demo/data"
set_global_assignment -name SEARCH_PATH "rtl"

foreach f {
    hw_demo/top_single_channel.v
    rtl/rrc_fir_core.v
    rtl/rrc_coeff_rom.v
    rtl/pipelined_adder_tree.v
    rtl/booth_mult_r4.v
} {
    set_global_assignment -name VERILOG_FILE $f
}

set_global_assignment -name SDC_FILE hw_demo/ep4ce6.sdc

set_global_assignment -name OPTIMIZATION_MODE "HIGH PERFORMANCE EFFORT"
set_global_assignment -name OPTIMIZATION_TECHNIQUE SPEED
set_global_assignment -name ALLOW_REGISTER_RETIMING ON
set_global_assignment -name AUTO_DSP_RECOGNITION OFF   ;# keep the Booth logic in the fabric
set_global_assignment -name NUM_PARALLEL_PROCESSORS ALL

execute_module -tool map
execute_module -tool fit
execute_module -tool sta

project_close

puts ""
puts "Compile finished."
puts "  fit report : $out_dir/${project_name}.fit.rpt"
puts "  sta report : $out_dir/${project_name}.sta.rpt"
puts "  next       : quartus_sta -t hw_demo/report_timing.tcl"
