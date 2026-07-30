//============================================================================
// rrc_coeff_rom -- root-raised-cosine matched-filter coefficient store.
//
// The taps are produced by python/gen_stimulus.py (unit-energy RRC, quantised
// to Q1.15).  The whole tap set is presented as one flattened bus so the FIR
// core can slice a constant coefficient per tap inside a generate loop; each
// slice constant-folds at synthesis, which is what turns the Booth multipliers
// into hardwired shift/add networks.
//
// Two equivalent implementations, selected at compile time:
//
//   default              $readmemh from COEFF_FILE.  The coefficient file is
//                        written without comment lines so the same file feeds
//                        both simulation and Quartus ROM inference.
//
//   `define RRC_COEFF_CASE
//                        a literal `case` statement included from
//                        data/rrc_coeffs_case.vh, for tools that will not
//                        infer a ROM from $readmemh.  Requires the generated
//                        file on the include path (iverilog/Quartus: -I data).
//
// Both are checked by sim/run_sim.sh, so neither path can rot.
//============================================================================
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module rrc_coeff_rom #(
    parameter N_TAPS      = 33,
    parameter COEFF_WIDTH = 16,
    parameter COEFF_FILE  = "data/rrc_coeffs.hex"
) (
    output wire [N_TAPS*COEFF_WIDTH-1:0] coeff_flat
);

    localparam ADDR_W = `RRC_CLOG2(N_TAPS);

    reg [COEFF_WIDTH-1:0] rom [0:N_TAPS-1];

`ifdef RRC_COEFF_CASE
    // ---- literal case-statement ROM --------------------------------------
    // An `initial` block, not `always @(*)`: the case statement reads only
    // constants, so a self-inferred sensitivity list would be empty and the
    // block would never fire.  A loop-driven initial block is also exactly the
    // form Quartus infers a constant ROM from.
    reg [ADDR_W-1:0]      addr;
    reg [COEFF_WIDTH-1:0] coeff;
    integer a;
    initial begin
        for (a = 0; a < N_TAPS; a = a + 1) begin
            addr = a[ADDR_W-1:0];
            `include "rrc_coeffs_case.vh"
            rom[a] = coeff;
        end
    end
`else
    // ---- $readmemh ROM ----------------------------------------------------
    initial begin
        $readmemh(COEFF_FILE, rom);
    end
`endif

    genvar k;
    generate
        for (k = 0; k < N_TAPS; k = k + 1) begin : g_flatten
            assign coeff_flat[k*COEFF_WIDTH +: COEFF_WIDTH] = rom[k];
        end
    endgenerate

    // synthesis translate_off
    integer c;
    initial begin
        #0;
        for (c = 0; c < N_TAPS; c = c + 1)
            if (^rom[c] === 1'bx) begin
                $display("FATAL %m: coefficient %0d is X -- '%s' missing or too short",
                         c, COEFF_FILE);
                $finish;
            end
    end
    // synthesis translate_on

endmodule
