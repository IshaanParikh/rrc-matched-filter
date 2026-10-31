#!/usr/bin/env bash
# at-speed self-test (hw_demo/dpl/rrc_bist.v) against python/dpl_bist.py's
# model, with a short run so it finishes in seconds. no EPT library needed.
# run from repo root: ./hw_demo/dpl/sim/run_bist_sim.sh
# env overrides: IVERILOG, PYTHON, LEN_LOG2 (default 12)
set -euo pipefail

cd "$(dirname "$0")/../../.."

PYTHON="${PYTHON:-python}"
if [ -z "${IVERILOG:-}" ] && [ -x /c/iverilog/bin/iverilog ]; then
    export PATH="/c/iverilog/bin:$PATH"
fi
IVERILOG="${IVERILOG:-iverilog}"
VVP="$(dirname "$(command -v "$IVERILOG")")/vvp"
LEN_LOG2="${LEN_LOG2:-12}"

if [ ! -f hw_demo/data/rrc_coeffs.hex ]; then
    "$PYTHON" python/gen_stimulus.py --span 5 --outdir hw_demo/data
fi

mkdir -p build/bist
"$PYTHON" python/dpl_bist.py --model-only --len-log2 "$LEN_LOG2" \
    --write-vh build/bist/bist_expected.vh

"$IVERILOG" -g2005 -Wall -I rtl -I build/bist -o build/bist/tb_rrc_bist.vvp \
    hw_demo/dpl/sim/tb_rrc_bist.v hw_demo/dpl/rrc_bist.v rtl/rrc_fir_core.v \
    rtl/rrc_coeff_rom.v rtl/pipelined_adder_tree.v rtl/booth_mult_r4.v
"$VVP" build/bist/tb_rrc_bist.vvp
