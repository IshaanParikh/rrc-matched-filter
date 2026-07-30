#!/usr/bin/env bash
#=============================================================================
# Full regression: stimulus -> golden model -> RTL simulation -> analysis.
# Run from the repository root:   ./sim/run_sim.sh
#
# Env overrides:
#   IVERILOG   path to iverilog   (default: iverilog, or C:/iverilog/bin on Windows)
#   PYTHON     python interpreter (default: python)
#   DUMP=1     also write data/tb_rrc_fir_axis.vcd
#=============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"
if [ -z "${IVERILOG:-}" ] && [ -x /c/iverilog/bin/iverilog ]; then
    export PATH="/c/iverilog/bin:$PATH"
fi
IVERILOG="${IVERILOG:-iverilog}"
VVP="$(dirname "$(command -v "$IVERILOG")")/vvp"

RTL="rtl/rrc_fir_axis.v rtl/rrc_fir_core.v rtl/rrc_coeff_rom.v \
     rtl/pipelined_adder_tree.v rtl/booth_mult_r4.v"
INC="-I rtl -I data"
BUILD="build"
PLUS=""
[ "${DUMP:-0}" = "1" ] && PLUS="+dump"

mkdir -p "$BUILD" data docs

hr() { printf '\n=== %s ===\n' "$1"; }

hr "1/6  Phase 1: stimulus generation"
"$PYTHON" python/gen_stimulus.py "$@"

hr "2/6  Phase 1b: bit-exact golden model"
"$PYTHON" python/golden_model.py

hr "3/6  Phase 3: Booth multiplier unit test"
"$IVERILOG" -g2005 -Wall $INC -o "$BUILD/tb_booth.vvp" sim/tb_booth_mult.v rtl/booth_mult_r4.v
"$VVP" "$BUILD/tb_booth.vvp"

hr "4/6  Phase 3: AXI4-Stream data-in-the-loop test"
"$IVERILOG" -g2005 -Wall $INC -o "$BUILD/tb_fir.vvp" sim/tb_rrc_fir_axis.v $RTL
"$VVP" "$BUILD/tb_fir.vvp" $PLUS

printf '\n--- same test, case-statement coefficient ROM ---\n'
"$IVERILOG" -g2005 -Wall -DRRC_COEFF_CASE $INC -o "$BUILD/tb_fir_case.vvp" sim/tb_rrc_fir_axis.v $RTL
"$VVP" "$BUILD/tb_fir_case.vvp" | tail -n 16

# The autoscaled link stimulus deliberately never reaches the Q1.15 rails, so
# it cannot cover the round-and-saturate stage.  This directed pattern does,
# and the same bit-exact golden comparison grades it.
printf '\n--- fixed-point corner cases (saturation coverage) ---\n'
"$PYTHON" python/gen_stimulus.py --outdir data_corner --pattern corner
"$PYTHON" python/golden_model.py --datadir data_corner
"$IVERILOG" -g2005 -Wall -I rtl -I data_corner -o "$BUILD/tb_fir_corner.vvp" sim/tb_rrc_fir_axis.v $RTL
"$VVP" "$BUILD/tb_fir_corner.vvp" | tail -n 14

hr "5/6  Phase 3: constellation analysis"
"$PYTHON" python/analyze_results.py

# A second pass over an essentially noise-free channel.  Same RTL, same
# testbench -- every path the testbench uses comes from stimulus_meta.vh, so
# pointing the include at another data directory re-targets the whole run.
# This is what shows the RRC TX/RX pair is ISI-free at the sampling instant:
# with the noise removed, anything left closing the eye is the filter's own
# doing, and the eye stays open.
hr "6/6  Phase 3: clean-channel run (zero-ISI check)"
"$PYTHON" python/gen_stimulus.py --outdir data_clean --esn0-db 40 --num-symbols 1024
"$PYTHON" python/golden_model.py --datadir data_clean
"$IVERILOG" -g2005 -Wall -I rtl -I data_clean -o "$BUILD/tb_fir_clean.vvp" sim/tb_rrc_fir_axis.v $RTL
"$VVP" "$BUILD/tb_fir_clean.vvp" | tail -n 16
"$PYTHON" python/analyze_results.py --datadir data_clean --tag clean_ --figures eye

printf '\nAll stages passed.\n'
