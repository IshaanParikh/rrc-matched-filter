//============================================================================
// rrc_fir_core -- one real-valued channel of the RRC matched filter.
//
//   din -> [ N_TAPS-deep delay line ]
//              |  |  |         (one tap per stage)
//              v  v  v
//          [ N_TAPS pipelined radix-4 Booth multipliers ]
//              |  |  |
//              v  v  v
//          [ pipelined adder tree, ceil(log2(N)) levels ]
//                   |
//                   v
//          [ round-half-up -> saturate -> register ] -> dout
//
// Bit growth
// ----------
//   din, coeff        Q1.15   16 bits
//   product           Q2.30   32 bits            (16 x 16)
//   accumulator       Q8.30   32 + ceil(log2(33)) = 38 bits
//   dout              Q1.15   16 bits            ((acc + 2^14) >>> 15, saturated)
//
// 38 bits is 1 bit more than the 37 the worst case actually needs
// (33 * 2^15 * 2^15 = 2^36.05), so the accumulator provably cannot wrap.
//
// Two enables, not one
// --------------------
//   sample_en : admit a new sample into the delay line
//   pipe_en   : advance every arithmetic pipeline register
//
// They differ when the stream has a gap (upstream not valid, downstream ready).
// The delay line must hold -- feeding it a bubble would corrupt the filter
// history -- while the arithmetic pipeline must keep advancing so in-flight
// results can drain to the output.  The bubbles that result carry garbage
// products, but the top level's valid pipeline marks them invalid, so nothing
// downstream ever sees them.  The reverse case cannot happen: the top level
// derives sample_en as (tvalid & pipe_en), so sample_en implies pipe_en.
//============================================================================
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module rrc_fir_core #(
    parameter N_TAPS      = 33,
    parameter DATA_WIDTH  = 16,
    parameter COEFF_WIDTH = 16,
    parameter COEFF_FILE  = "data/rrc_coeffs.hex"
) (
    input  wire                         clk,
    input  wire                         rst_n,       // synchronous, active low
    input  wire                         sample_en,   // advance the delay line
    input  wire                         pipe_en,     // advance the arithmetic pipeline
    input  wire signed [DATA_WIDTH-1:0] din,
    output wire signed [DATA_WIDTH-1:0] dout
);

    localparam PROD_WIDTH  = DATA_WIDTH + COEFF_WIDTH;        // 32
    localparam TREE_LEVELS = `RRC_CLOG2(N_TAPS);              // 6
    localparam ACC_WIDTH   = PROD_WIDTH + TREE_LEVELS;        // 38
    localparam FRAC_BITS   = `RRC_FRAC_BITS;                  // 15
    localparam SHIFT_W     = ACC_WIDTH - FRAC_BITS;           // 23

    // delay line (1) + Booth multiplier (4) + adder tree (6) + output reg (1)
    localparam LATENCY = 1 + `BOOTH_R4_LATENCY + TREE_LEVELS + 1;

    // ---------------------------------------------------------------------
    // Delay line.  No initial value and a synchronous reset, so the outputs
    // are X at power-up in simulation until reset clears the pipeline -- the
    // X-propagation check the testbench relies on.
    // ---------------------------------------------------------------------
    reg signed [DATA_WIDTH-1:0] shift_reg [0:N_TAPS-1];

    integer t;
    always @(posedge clk) begin
        if (!rst_n) begin
            for (t = 0; t < N_TAPS; t = t + 1)
                shift_reg[t] <= {DATA_WIDTH{1'b0}};
        end else if (sample_en) begin
            shift_reg[0] <= din;                      // newest sample
            for (t = 1; t < N_TAPS; t = t + 1)
                shift_reg[t] <= shift_reg[t-1];
        end
    end

    // ---------------------------------------------------------------------
    // Coefficients.
    // ---------------------------------------------------------------------
    wire [N_TAPS*COEFF_WIDTH-1:0] coeff_flat;

    rrc_coeff_rom #(
        .N_TAPS      (N_TAPS),
        .COEFF_WIDTH (COEFF_WIDTH),
        .COEFF_FILE  (COEFF_FILE)
    ) u_coeff_rom (
        .coeff_flat (coeff_flat)
    );

    // ---------------------------------------------------------------------
    // One pipelined Booth MAC per tap.
    // ---------------------------------------------------------------------
    wire [N_TAPS*PROD_WIDTH-1:0] prod_flat;

    genvar k;
    generate
        for (k = 0; k < N_TAPS; k = k + 1) begin : g_mac
            wire signed [PROD_WIDTH-1:0] prod;

            booth_mult_r4 #(
                .A_WIDTH (DATA_WIDTH),
                .B_WIDTH (COEFF_WIDTH)
            ) u_mult (
                .clk   (clk),
                .rst_n (rst_n),
                .en    (pipe_en),
                .a     (shift_reg[k]),
                .b     ($signed(coeff_flat[k*COEFF_WIDTH +: COEFF_WIDTH])),
                .p     (prod)
            );

            assign prod_flat[k*PROD_WIDTH +: PROD_WIDTH] = prod;
        end
    endgenerate

    // ---------------------------------------------------------------------
    // Adder tree.
    // ---------------------------------------------------------------------
    wire signed [ACC_WIDTH-1:0] acc;

    pipelined_adder_tree #(
        .N    (N_TAPS),
        .IN_W (PROD_WIDTH)
    ) u_tree (
        .clk      (clk),
        .rst_n    (rst_n),
        .en       (pipe_en),
        .din_flat (prod_flat),
        .dout     (acc)
    );

    // ---------------------------------------------------------------------
    // Round half up, then saturate, then register.
    //
    // rnd_const is declared as a signed vector rather than written as
    // (1 << 14): an unsigned literal would make the whole addition unsigned
    // and silently break the arithmetic for negative accumulators.
    // ---------------------------------------------------------------------
    wire signed [ACC_WIDTH-1:0] rnd_const =
        {{(ACC_WIDTH-FRAC_BITS){1'b0}}, 1'b1, {(FRAC_BITS-1){1'b0}}};   // +2^14

    wire signed [ACC_WIDTH-1:0] acc_rnd = acc + rnd_const;
    wire signed [SHIFT_W-1:0]   y_full  = acc_rnd >>> FRAC_BITS;

    // y_full fits in DATA_WIDTH bits iff its top (SHIFT_W-DATA_WIDTH+1) bits
    // are all copies of the sign bit.
    wire [SHIFT_W-DATA_WIDTH : 0] guard = y_full[SHIFT_W-1 : DATA_WIDTH-1];
    wire                          ovf   = ~((&guard) | (~|guard));

    wire signed [DATA_WIDTH-1:0] y_sat =
        ovf ? (y_full[SHIFT_W-1] ? {1'b1, {(DATA_WIDTH-1){1'b0}}}     // -1.0
                                 : {1'b0, {(DATA_WIDTH-1){1'b1}}})    // +1.0 - 2^-15
            : y_full[DATA_WIDTH-1:0];

    reg signed [DATA_WIDTH-1:0] dout_r;
    always @(posedge clk) begin
        if (!rst_n)        dout_r <= {DATA_WIDTH{1'b0}};
        else if (pipe_en)  dout_r <= y_sat;
    end

    assign dout = dout_r;

endmodule
