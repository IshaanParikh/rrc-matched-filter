# Timing constraints for the scoped-down single-channel demo on EP4CE6E22C8N.
# Same 250 MHz target and I/O budget methodology as syn/rrc_fir.sdc, just
# pointed at the smaller top level so Fmax here is directly comparable.

create_clock -name clk -period 4.000 [get_ports clk]
create_clock -name clk_virt -period 4.000

derive_clock_uncertainty

set data_inputs [remove_from_collection [all_inputs] [get_ports clk]]

set_input_delay  -clock clk_virt -max 1.200 $data_inputs
set_input_delay  -clock clk_virt -min 0.200 $data_inputs

set_output_delay -clock clk_virt -max 1.200 [all_outputs]
set_output_delay -clock clk_virt -min 0.200 [all_outputs]
