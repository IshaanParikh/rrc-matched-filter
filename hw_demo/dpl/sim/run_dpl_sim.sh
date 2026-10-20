#!/usr/bin/env bash
# DueProLogic top level through EPT's Active Transfer library, end to end.
# run from repo root: ./hw_demo/dpl/sim/run_dpl_sim.sh
#
# needs EPT's library sources copied into hw_demo/dpl/ept/src/ (see the
# README there) and the 21-tap coefficients, which this script generates.
# env overrides: IVERILOG, PYTHON, DUMP=1
set -euo pipefail

cd "$(dirname "$0")/../../.."

PYTHON="${PYTHON:-python}"
if [ -z "${IVERILOG:-}" ] && [ -x /c/iverilog/bin/iverilog ]; then
    export PATH="/c/iverilog/bin:$PATH"
fi
IVERILOG="${IVERILOG:-iverilog}"
VVP="$(dirname "$(command -v "$IVERILOG")")/vvp"

EPT="hw_demo/dpl/ept/src"
EPT_FILES="active_serial_library.v active_transfer_uart.v ft_245_state_machine.v
           endpoint_registers.v active_trigger.v active_transfer.v
           uart_top.v uart_receiver.v uart_transmitter.v eptWireOR.v"

for f in $EPT_FILES define.v; do
    if [ ! -f "$EPT/$f" ]; then
        echo "missing $EPT/$f - copy EPT's Active Transfer library there first," >&2
        echo "see hw_demo/dpl/ept/README.md" >&2
        exit 1
    fi
done

if [ ! -f hw_demo/data/rrc_coeffs.hex ]; then
    "$PYTHON" python/gen_stimulus.py --span 5 --outdir hw_demo/data
fi

RTL="hw_demo/dpl/dpl_top.v rtl/rrc_fir_core.v rtl/rrc_coeff_rom.v
     rtl/pipelined_adder_tree.v rtl/booth_mult_r4.v"

# eptWireOR.v ends with `default_nettype none, so it goes last
EPT_SRCS=""
for f in $EPT_FILES; do EPT_SRCS="$EPT_SRCS $EPT/$f"; done

PLUS=""
[ "${DUMP:-0}" = "1" ] && PLUS="+dump"

mkdir -p build
"$IVERILOG" -g2005 -I rtl -I "$EPT" -o build/tb_dpl_top.vvp \
    hw_demo/dpl/sim/tb_dpl_top.v $RTL $EPT_SRCS
"$VVP" build/tb_dpl_top.vvp $PLUS
