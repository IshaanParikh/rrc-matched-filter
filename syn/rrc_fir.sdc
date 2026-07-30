#=============================================================================
# rrc_fir.sdc -- timing constraints for the AXI4-Stream RRC matched filter.
#
# Target: 250 MHz (4.000 ns), the rate at which a 4x-oversampled link runs the
# filter at 62.5 Msym/s.
#
# The number that matters for this design is the Fmax Summary, which TimeQuest
# derives from register-to-register paths only.  The I/O constraints below exist
# so the report is not littered with unconstrained-path warnings; in a real
# system this block sits between other AXI4-Stream IP inside the fabric and
# never reaches a pin, so treat the I/O numbers as budget, not truth.
#=============================================================================

#---------------------------------------------------------------------------
# Clock
#---------------------------------------------------------------------------
create_clock -name clk -period 4.000 [get_ports clk]

# A virtual clock to budget I/O against, so pad delays do not contaminate the
# core Fmax figure.
create_clock -name clk_virt -period 4.000

derive_clock_uncertainty

#---------------------------------------------------------------------------
# I/O budget: 30% of the period each way.
#---------------------------------------------------------------------------
set data_inputs [remove_from_collection [all_inputs] [get_ports clk]]

set_input_delay  -clock clk_virt -max 1.200 $data_inputs
set_input_delay  -clock clk_virt -min 0.200 $data_inputs

set_output_delay -clock clk_virt -max 1.200 [all_outputs]
set_output_delay -clock clk_virt -min 0.200 [all_outputs]

#---------------------------------------------------------------------------
# Reset
#
# rst_n is synchronous and is sampled by the same clock as everything else, so
# it is a normal timed path -- deliberately NOT false-pathed.  The whole
# X-propagation guarantee the testbench checks depends on reset being timed.
#---------------------------------------------------------------------------
