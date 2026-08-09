// one real channel of the RRC matched filter
//
//   din -> 33-tap delay line -> 33 booth multipliers -> adder tree
//       -> round half up -> saturate -> dout
//
// widths: din/coeff Q1.15 (16b), product Q2.30 (32b), accumulator Q8.30 (38b),
// dout Q1.15 (16b). worst case accumulator is 33 * 2^15 * 2^15 = 2^35.04,
// needs 37 bits, we have 38 so it can't wrap.
//
// two enables:
//   sample_en - load a new sample into the delay line
//   pipe_en   - advance the arithmetic pipeline
// they differ on a gap in the stream (source idle, sink ready): the delay
// line has to hold or it corrupts the filter history, but the arithmetic
// pipeline keeps moving so in-flight results can drain. the bubbles carry
// garbage but the top level marks them invalid.
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module rrc_fir_core #(
    parameter N_TAPS      = 33,
    parameter DATA_WIDTH  = 16,
    parameter COEFF_WIDTH = 16,
    parameter COEFF_FILE  = "data/rrc_coeffs.hex"
) (
    input  wire                         clk,
    input  wire                         rst_n,       // sync, active low
    input  wire                         sample_en,   // advance delay line
    input  wire                         pipe_en,     // advance arithmetic pipe
    input  wire signed [DATA_WIDTH-1:0] din,
    output wire signed [DATA_WIDTH-1:0] dout
);

    localparam PROD_WIDTH  = DATA_WIDTH + COEFF_WIDTH;
    localparam TREE_LEVELS = `RRC_CLOG2(N_TAPS);
    localparam ACC_WIDTH   = PROD_WIDTH + TREE_LEVELS;
    localparam FRAC_BITS   = `RRC_FRAC_BITS;
    localparam SHIFT_W     = ACC_WIDTH - FRAC_BITS;

    // delay line (1) + booth (4) + tree (6) + output reg (1)
    localparam LATENCY = 1 + `BOOTH_R4_LATENCY + TREE_LEVELS + 1;

    // delay line. no initial value + sync reset, so it's X at power-up
    // until reset clears it - the testbench checks for that.
    reg signed [DATA_WIDTH-1:0] shift_reg [0:N_TAPS-1];

    integer t;
    always @(posedge clk) begin
        if (!rst_n) begin
            for (t = 0; t < N_TAPS; t = t + 1)
                shift_reg[t] <= {DATA_WIDTH{1'b0}};
        end else if (sample_en) begin
            shift_reg[0] <= din;
            for (t = 1; t < N_TAPS; t = t + 1)
                shift_reg[t] <= shift_reg[t-1];
        end
    end

    wire [N_TAPS*COEFF_WIDTH-1:0] coeff_flat;

    rrc_coeff_rom #(
        .N_TAPS      (N_TAPS),
        .COEFF_WIDTH (COEFF_WIDTH),
        .COEFF_FILE  (COEFF_FILE)
    ) u_coeff_rom (
        .coeff_flat (coeff_flat)
    );

    // one booth MAC per tap
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

    // round half up, saturate, register
    // rnd_const is a signed vector, not (1 << 14) - an unsigned literal would
    // make the add unsigned and break negative accumulators
    wire signed [ACC_WIDTH-1:0] rnd_const =
        {{(ACC_WIDTH-FRAC_BITS){1'b0}}, 1'b1, {(FRAC_BITS-1){1'b0}}};

    wire signed [ACC_WIDTH-1:0] acc_rnd = acc + rnd_const;
    wire signed [SHIFT_W-1:0]   y_full  = acc_rnd >>> FRAC_BITS;

    // fits in 16 bits only if everything above bit 15 matches the sign bit
    wire [SHIFT_W-DATA_WIDTH : 0] guard = y_full[SHIFT_W-1 : DATA_WIDTH-1];
    wire                          ovf   = ~((&guard) | (~|guard));

    wire signed [DATA_WIDTH-1:0] y_sat =
        ovf ? (y_full[SHIFT_W-1] ? {1'b1, {(DATA_WIDTH-1){1'b0}}}
                                 : {1'b0, {(DATA_WIDTH-1){1'b1}}})
            : y_full[DATA_WIDTH-1:0];

    reg signed [DATA_WIDTH-1:0] dout_r;
    always @(posedge clk) begin
        if (!rst_n)        dout_r <= {DATA_WIDTH{1'b0}};
        else if (pipe_en)  dout_r <= y_sat;
    end

    assign dout = dout_r;

endmodule
