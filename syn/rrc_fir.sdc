# timing constraints for the RRC filter, target 250 MHz (4 ns period)
# what matters is the Fmax summary (register-to-register paths only), the
# I/O constraints below just stop the report from filling with warnings

create_clock -name clk -period 4.000 [get_ports clk]

# virtual clock for budgeting I/O so pad delays don't pollute Fmax
create_clock -name clk_virt -period 4.000

derive_clock_uncertainty

# 30% of the period each way
set data_inputs [remove_from_collection [all_inputs] [get_ports clk]]

set_input_delay  -clock clk_virt -max 1.200 $data_inputs
set_input_delay  -clock clk_virt -min 0.200 $data_inputs

set_output_delay -clock clk_virt -max 1.200 [all_outputs]
set_output_delay -clock clk_virt -min 0.200 [all_outputs]

# rst_n is synchronous, not false-pathed on purpose since the X-propagation
# check in the testbench depends on reset being a timed path
