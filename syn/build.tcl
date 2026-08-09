# quartus project + full compile for the RRC filter
# run from repo root so the $readmemh path in rrc_coeff_rom.v resolves:
#     quartus_sh -t syn/build.tcl
#     quartus_sta -t syn/report_timing.tcl
#
# override device: quartus_sh -t syn/build.tcl -device X -family Y

package require ::quartus::project
package require ::quartus::flow

# defaults
set project_name "rrc_fir_axis"
set top_level    "rrc_fir_axis"
# Cyclone V SE on the DE1-SoC, speed grade 6
set device       "5CSEMA5F31C6"
set family       "Cyclone V"
set out_dir      "syn/output"

# argument parsing
for {set i 0} {$i < [llength $quartus(args)]} {incr i} {
    set arg [lindex $quartus(args) $i]
    switch -- $arg {
        -device  { incr i; set device  [lindex $quartus(args) $i] }
        -family  { incr i; set family  [lindex $quartus(args) $i] }
        -out     { incr i; set out_dir [lindex $quartus(args) $i] }
    }
}

file mkdir $out_dir

# project
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

# so $readmemh / `include can find the generated coefficient files
set_global_assignment -name SEARCH_PATH "data"
set_global_assignment -name SEARCH_PATH "rtl"

# source files
foreach f {
    rtl/rrc_fir_axis.v
    rtl/rrc_fir_core.v
    rtl/rrc_coeff_rom.v
    rtl/pipelined_adder_tree.v
    rtl/booth_mult_r4.v
} {
    set_global_assignment -name VERILOG_FILE $f
}

set_global_assignment -name SDC_FILE syn/rrc_fir.sdc

# optimize for speed, allow retiming across the pipeline registers
set_global_assignment -name OPTIMIZATION_MODE "HIGH PERFORMANCE EFFORT"
set_global_assignment -name OPTIMIZATION_TECHNIQUE SPEED
set_global_assignment -name ALLOW_REGISTER_RETIMING ON
set_global_assignment -name AUTO_DSP_RECOGNITION OFF   ;# keep the Booth logic in fabric
set_global_assignment -name NUM_PARALLEL_PROCESSORS ALL

# compile
execute_module -tool map
execute_module -tool fit
execute_module -tool sta
execute_module -tool asm -args "--analysis_and_elaboration" ;# harmless if skipped

project_close

puts ""
puts "Compile finished."
puts "  fit report : $out_dir/${project_name}.fit.rpt   (resource utilisation)"
puts "  sta report : $out_dir/${project_name}.sta.rpt"
puts "  next       : quartus_sta -t syn/report_timing.tcl"
