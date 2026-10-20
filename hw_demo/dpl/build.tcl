# Quartus project + compile for the DueProLogic board build: dpl_top.v,
# one 21-tap rrc_fir_core channel plus EPT's Active Transfer library, with
# real pin assignments. Produces a .sof (JTAG) and a .pof for the board's
# configuration flash (EPT-Blaster, Active Serial).
#
# Run from the repository root, after copying EPT's library into
# hw_demo/dpl/ept/src/ (see hw_demo/dpl/ept/README.md):
#     python python/gen_stimulus.py --span 5 --outdir hw_demo/data
#     quartus_sh -t hw_demo/dpl/build.tcl
#
# options:
#     -config-device EPCS4|EPCS1   flash the .pof targets (default EPCS4)
#     -out <dir>                   output directory (default hw_demo/dpl/output)

package require ::quartus::project
package require ::quartus::flow

set project_name  "rrc_fir_dpl"
set top_level     "dpl_top"
set device        "EP4CE6E22C8"
set family        "Cyclone IV E"
set out_dir       "hw_demo/dpl/output"
set ept_dir       "hw_demo/dpl/ept/src"
# DS says the board's config flash is an M25P40, which is what an EPCS4 is.
# EPT's own demo projects say EPCS1; see the README if the Programmer
# rejects the flash's silicon ID.
set config_device "EPCS4"

for {set i 0} {$i < [llength $quartus(args)]} {incr i} {
    set arg [lindex $quartus(args) $i]
    switch -- $arg {
        -config-device { incr i; set config_device [lindex $quartus(args) $i] }
        -out           { incr i; set out_dir       [lindex $quartus(args) $i] }
    }
}

# EPT's library is not in the repo. Fail here with a useful message rather
# than in Analysis & Synthesis with a missing-module error.
set ept_files {
    active_serial_library.v
    active_transfer_uart.v
    ft_245_state_machine.v
    endpoint_registers.v
    active_trigger.v
    active_transfer.v
    uart_top.v
    uart_receiver.v
    uart_transmitter.v
    eptWireOR.v
}
foreach f [concat $ept_files define.v] {
    if {![file exists "$ept_dir/$f"]} {
        post_message -type error "missing $ept_dir/$f: copy EPT's Active Transfer library there, see hw_demo/dpl/ept/README.md"
        qexit -error
    }
}
if {![file exists "hw_demo/data/rrc_coeffs.hex"]} {
    post_message -type error "missing hw_demo/data/rrc_coeffs.hex: run python python/gen_stimulus.py --span 5 --outdir hw_demo/data"
    qexit -error
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

set_global_assignment -name SEARCH_PATH "rtl"
set_global_assignment -name SEARCH_PATH "hw_demo/data"
set_global_assignment -name SEARCH_PATH $ept_dir

foreach f {
    hw_demo/dpl/dpl_top.v
    rtl/rrc_fir_core.v
    rtl/rrc_coeff_rom.v
    rtl/pipelined_adder_tree.v
    rtl/booth_mult_r4.v
} {
    set_global_assignment -name VERILOG_FILE $f
}
# eptWireOR.v ends with `default_nettype none, keep it last
foreach f $ept_files {
    set_global_assignment -name VERILOG_FILE "$ept_dir/$f"
}

set_global_assignment -name SDC_FILE hw_demo/dpl/dpl.sdc
source hw_demo/dpl/dpl_pins.tcl

# configuration: Active Serial from the on-board flash, programmed by
# EPT-Blaster. Setting the configuration device makes the Assembler write
# the .pof next to the .sof.
set_global_assignment -name USE_CONFIGURATION_DEVICE ON
set_global_assignment -name CYCLONEIII_CONFIGURATION_DEVICE $config_device
set_global_assignment -name ON_CHIP_BITSTREAM_DECOMPRESSION ON
set_global_assignment -name CYCLONEII_RESERVE_NCEO_AFTER_CONFIGURATION "USE AS REGULAR IO"
# unused pins include the other 32 LEDs and the rest of the FT2232H bus:
# leave them as inputs with weak pull-ups, which keeps the LEDs dark
set_global_assignment -name RESERVE_ALL_UNUSED_PINS_WEAK_PULLUP "AS INPUT TRI-STATED WITH WEAK PULL-UP"

set_global_assignment -name OPTIMIZATION_MODE "HIGH PERFORMANCE EFFORT"
set_global_assignment -name AUTO_DSP_RECOGNITION OFF   ;# keep the Booth logic in the fabric
set_global_assignment -name NUM_PARALLEL_PROCESSORS ALL

execute_module -tool map
execute_module -tool fit
execute_module -tool sta
execute_module -tool asm

project_close

puts ""
puts "Compile finished."
puts "  fit report : $out_dir/${project_name}.fit.rpt"
puts "  sta report : $out_dir/${project_name}.sta.rpt"
puts "  JTAG image : $out_dir/${project_name}.sof"
puts "  flash image: $out_dir/${project_name}.pof  ($config_device, program with EPT-Blaster in Active Serial mode)"
