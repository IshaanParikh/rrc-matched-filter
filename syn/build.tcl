#=============================================================================
# build.tcl -- Quartus Prime project creation + full compile for the RRC filter.
#
# Run FROM THE REPOSITORY ROOT so the $readmemh path in rrc_coeff_rom.v
# ("data/rrc_coeffs.hex") resolves:
#
#     quartus_sh -t syn/build.tcl
#     quartus_sta -t syn/report_timing.tcl        ;# Fmax + critical path
#
# Override the device without editing this file:
#     quartus_sh -t syn/build.tcl -device 5CGXFC9E7F35C8 -family "Cyclone V"
#=============================================================================

package require ::quartus::project
package require ::quartus::flow

# --- defaults ---------------------------------------------------------------
set project_name "rrc_fir_axis"
set top_level    "rrc_fir_axis"
# 5CSEMA5F31C6 is the Cyclone V SE on the DE1-SoC: 32,070 ALMs, speed grade 6
# (the fastest commercial grade, which is what a 250 MHz target wants).
set device       "5CSEMA5F31C6"
set family       "Cyclone V"
set out_dir      "syn/output"

# --- argument parsing -------------------------------------------------------
for {set i 0} {$i < [llength $quartus(args)]} {incr i} {
    set arg [lindex $quartus(args) $i]
    switch -- $arg {
        -device  { incr i; set device  [lindex $quartus(args) $i] }
        -family  { incr i; set family  [lindex $quartus(args) $i] }
        -out     { incr i; set out_dir [lindex $quartus(args) $i] }
    }
}

file mkdir $out_dir

# --- project ----------------------------------------------------------------
if {[project_exists $project_name]} {
    project_open $project_name -revision $project_name
} else {
    project_new $project_name -revision $project_name -overwrite
}

set_global_assignment -name FAMILY $family
set_global_assignment -name DEVICE $device
set_global_assignment -name TOP_LEVEL_ENTITY $top_level
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY $out_dir

# Verilog-2001; the RTL uses no SystemVerilog constructs.
set_global_assignment -name VERILOG_INPUT_VERSION VERILOG_2001

# So $readmemh and `include can find the generated coefficient files.
set_global_assignment -name SEARCH_PATH "data"
set_global_assignment -name SEARCH_PATH "rtl"

# --- source files -----------------------------------------------------------
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

# --- optimisation -----------------------------------------------------------
# Speed, and let the fitter retime across the pipeline registers that were put
# in deliberately for exactly that purpose.
set_global_assignment -name OPTIMIZATION_MODE "HIGH PERFORMANCE EFFORT"
set_global_assignment -name OPTIMIZATION_TECHNIQUE SPEED
set_global_assignment -name ALLOW_REGISTER_RETIMING ON
set_global_assignment -name AUTO_DSP_RECOGNITION OFF   ;# keep the Booth logic in the fabric
set_global_assignment -name NUM_PARALLEL_PROCESSORS ALL

# AUTO_DSP_RECOGNITION is off on purpose.  The point of the exercise is a custom
# radix-4 Booth multiplier built from logic elements; letting the synthesiser
# swap in hard DSP blocks would delete the thing being measured.  Turn it ON to
# see what the hardened multipliers cost instead.

# --- compile ----------------------------------------------------------------
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
