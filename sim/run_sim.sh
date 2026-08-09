#!/usr/bin/env bash
# full regression: stimulus -> golden model -> RTL sim -> analysis
# run from repo root: ./sim/run_sim.sh
#
# env overrides: IVERILOG, PYTHON, DUMP=1 (also writes data/tb_rrc_fir_axis.vcd)
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

hr "1/6  stimulus generation"
"$PYTHON" python/gen_stimulus.py "$@"

hr "2/6  bit-exact golden model"
"$PYTHON" python/golden_model.py

hr "3/6  Booth multiplier unit test"
"$IVERILOG" -g2005 -Wall $INC -o "$BUILD/tb_booth.vvp" sim/tb_booth_mult.v rtl/booth_mult_r4.v
"$VVP" "$BUILD/tb_booth.vvp"

hr "4/6  AXI4-Stream data-in-the-loop test"
"$IVERILOG" -g2005 -Wall $INC -o "$BUILD/tb_fir.vvp" sim/tb_rrc_fir_axis.v $RTL
"$VVP" "$BUILD/tb_fir.vvp" $PLUS

printf '\n--- same test, case-statement coefficient ROM ---\n'
"$IVERILOG" -g2005 -Wall -DRRC_COEFF_CASE $INC -o "$BUILD/tb_fir_case.vvp" sim/tb_rrc_fir_axis.v $RTL
"$VVP" "$BUILD/tb_fir_case.vvp" | tail -n 16

# the link stimulus never hits the Q1.15 rails, so it never exercises
# round-and-saturate. this pattern does.
printf '\n--- fixed-point corner cases (saturation coverage) ---\n'
"$PYTHON" python/gen_stimulus.py --outdir data_corner --pattern corner
"$PYTHON" python/golden_model.py --datadir data_corner
"$IVERILOG" -g2005 -Wall -I rtl -I data_corner -o "$BUILD/tb_fir_corner.vvp" sim/tb_rrc_fir_axis.v $RTL
"$VVP" "$BUILD/tb_fir_corner.vvp" | tail -n 14

hr "5/6  constellation analysis"
"$PYTHON" python/analyze_results.py

# same RTL and testbench, just pointed at a near noise-free data dir. if the
# eye still stays open with the noise gone, that's the zero-ISI property.
hr "6/6  clean-channel run (zero-ISI check)"
"$PYTHON" python/gen_stimulus.py --outdir data_clean --esn0-db 40 --num-symbols 1024
"$PYTHON" python/golden_model.py --datadir data_clean
"$IVERILOG" -g2005 -Wall -I rtl -I data_clean -o "$BUILD/tb_fir_clean.vvp" sim/tb_rrc_fir_axis.v $RTL
"$VVP" "$BUILD/tb_fir_clean.vvp" | tail -n 16
"$PYTHON" python/analyze_results.py --datadir data_clean --tag clean_ --figures eye

printf '\nAll stages passed.\n'
