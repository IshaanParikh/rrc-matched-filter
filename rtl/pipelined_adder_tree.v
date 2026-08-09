// balanced signed adder tree, one register stage per level, works for any N
//
// latency is ceil(log2(N)) cycles, output width is IN_W + ceil(log2(N)) bits
// (exactly the growth from summing N signed values, so no overflow)
//
// N=33 goes 33 -> 17 -> 9 -> 5 -> 3 -> 2 -> 1. odd levels just pass the
// leftover node through to the next level instead of dropping it.
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module pipelined_adder_tree #(
    parameter N    = 33,      // number of signed inputs
    parameter IN_W = 32       // width of each input
) (
    input  wire                                    clk,
    input  wire                                    rst_n,   // sync, active low
    input  wire                                    en,      // pipeline advance
    input  wire [N*IN_W-1:0]                       din_flat,
    output wire signed [IN_W + `RRC_CLOG2(N) - 1 : 0] dout
);

    localparam LEVELS = `RRC_CLOG2(N);
    localparam OUT_W  = IN_W + LEVELS;

    // unpack + sign extend to the accumulator width
    reg signed [OUT_W-1:0] lvl0 [0:N-1];
    integer j;
    always @(*) begin
        for (j = 0; j < N; j = j + 1)
            lvl0[j] = $signed(din_flat[j*IN_W +: IN_W]);
    end

    // one registered add per level
    reg signed [OUT_W-1:0] node [1:LEVELS][0:N-1];

    integer L, i, nprev, nlive;
    always @(posedge clk) begin
        if (!rst_n) begin
            for (L = 1; L <= LEVELS; L = L + 1)
                for (i = 0; i < N; i = i + 1)
                    node[L][i] <= {OUT_W{1'b0}};
        end else if (en) begin
            nlive = (N + 1) >> 1;
            for (i = 0; i < N; i = i + 1) begin
                if (i < nlive) begin
                    if (2*i + 1 < N) node[1][i] <= lvl0[2*i] + lvl0[2*i + 1];
                    else             node[1][i] <= lvl0[2*i];          // odd one out
                end
            end
            for (L = 2; L <= LEVELS; L = L + 1) begin
                nprev = (N + (1 << (L-1)) - 1) >> (L-1);   // ceil(N / 2^(L-1))
                nlive = (nprev + 1) >> 1;
                for (i = 0; i < N; i = i + 1) begin
                    if (i < nlive) begin
                        if (2*i + 1 < nprev) node[L][i] <= node[L-1][2*i] + node[L-1][2*i + 1];
                        else                 node[L][i] <= node[L-1][2*i];
                    end
                end
            end
        end
    end

    assign dout = node[LEVELS][0];

endmodule
