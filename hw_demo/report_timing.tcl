# Pull Fmax and resource numbers for the scoped-down single-channel demo.
# Run from the repository root, after hw_demo/build.tcl:
#     quartus_sta -t hw_demo/report_timing.tcl

package require ::quartus::project
package require ::quartus::sta

set project_name "rrc_fir_single_ep4ce6"
set out_dir      "hw_demo/output"
file mkdir $out_dir

project_open $project_name -revision $project_name

create_timing_netlist -model slow
read_sdc
update_timing_netlist

report_clock_fmax_summary -file "$out_dir/fmax.rpt"

report_timing -setup -npaths 20 -detail full_path \
    -file "$out_dir/critical_path.rpt"

set paths [get_timing_paths -setup -npaths 1]
foreach_in_collection p $paths {
    set slack [get_path_info $p -slack]
    puts ""
    puts "worst-case setup slack: $slack ns"
    if {$slack < 0} {
        puts "SETUP FAILS at the constrained period, see $out_dir/critical_path.rpt"
    } else {
        puts "setup MET at the constrained period"
    }
}

delete_timing_netlist
project_close

puts ""
puts "Reports written to $out_dir/{fmax,critical_path}.rpt"
puts "Resource utilisation is in $out_dir/${project_name}.fit.rpt"
