// RRC coefficient store. taps come from python/gen_stimulus.py, quantised to
// Q1.15. presented as one flat bus so the FIR core can slice a constant per
// tap - each slice folds at synthesis time, turning the booth multipliers
// into fixed shift/add networks.
//
// default is $readmemh from COEFF_FILE. define RRC_COEFF_CASE to use a
// literal case statement from data/rrc_coeffs_case.vh instead, for tools
// that won't infer a ROM from $readmemh.
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
    // initial block, not always @(*) - the case only reads constants so an
    // inferred sensitivity list would be empty and this would never fire
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
                $display("FATAL %m: coefficient %0d is X, '%s' missing or too short",
                         c, COEFF_FILE);
                $finish;
            end
    end
    // synthesis translate_on

endmodule
