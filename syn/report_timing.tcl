#=============================================================================
# report_timing.tcl -- extract the two portfolio metrics after a compile.
#
# Run FROM THE REPOSITORY ROOT, after syn/build.tcl:
#     quartus_sta -t syn/report_timing.tcl
#
# Writes:
#   syn/output/fmax.rpt           TimeQuest Fmax summary (register-to-register)
#   syn/output/critical_path.rpt  the 20 worst setup paths, full detail
#   syn/output/resources.rpt      ALM / register / DSP / memory usage
#
# If setup fails at 250 MHz, critical_path.rpt names the stage to cut.  The
# design is built to absorb that: booth_mult_r4's compression tree and
# pipelined_adder_tree both take one adder per stage already, so the usual next
# move is to split the partial-product select (S2) into its own register stage,
# which costs one cycle of latency and no functional change.
#=============================================================================

package require ::quartus::project
package require ::quartus::sta

set project_name "rrc_fir_axis"
set out_dir      "syn/output"
file mkdir $out_dir

project_open $project_name -revision $project_name

# The slow-corner model at the hottest temperature is the one that decides
# whether the design closes.
create_timing_netlist -model slow
read_sdc
update_timing_netlist

# --- Fmax -------------------------------------------------------------------
report_clock_fmax_summary -file "$out_dir/fmax.rpt"

# --- critical path ----------------------------------------------------------
report_timing -setup -npaths 20 -detail full_path \
    -file "$out_dir/critical_path.rpt"

# --- worst slack, to stdout so it lands in the console log ------------------
set paths [get_timing_paths -setup -npaths 1]
foreach_in_collection p $paths {
    set slack [get_path_info $p -slack]
    puts ""
    puts "worst-case setup slack: $slack ns"
    if {$slack < 0} {
        puts "SETUP FAILS at the constrained period -- see $out_dir/critical_path.rpt"
    } else {
        puts "setup MET at the constrained period"
    }
}

delete_timing_netlist
project_close

puts ""
puts "Reports written to $out_dir/{fmax,critical_path}.rpt"
puts "Resource utilisation is in $out_dir/${project_name}.fit.rpt"
puts "  (search for 'Fitter Resource Usage Summary')"
