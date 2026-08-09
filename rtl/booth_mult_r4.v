// radix-4 booth multiplier, 16x16 -> 32, pipelined over 4 stages
//
// booth recoding turns the 16-bit multiplier into 8 digits in {-2,-1,0,1,2}:
//   B = sum(i=0..7) d_i * 4^i,  d_i = -2*b[2i+1] + b[2i] + b[2i-1], b[-1]=0
// so each partial product is a shift/negate instead of a real multiply
//
// the 4 possible multiples (+-A, +-2A) get computed once in S1 and shared by
// all 8 muxes, so each stage is just a mux + an add
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module booth_mult_r4 #(
    parameter A_WIDTH = 16,     // multiplicand (data sample)
    parameter B_WIDTH = 16      // multiplier (coefficient), must be even
) (
    input  wire                              clk,
    input  wire                              rst_n,    // sync, active low
    input  wire                              en,       // pipeline advance
    input  wire signed [A_WIDTH-1:0]         a,
    input  wire signed [B_WIDTH-1:0]         b,
    output wire signed [A_WIDTH+B_WIDTH-1:0] p
);

    localparam P_WIDTH = A_WIDTH + B_WIDTH;   // 32
    localparam N_PP    = B_WIDTH / 2;         // 8 partial products
    localparam E_WIDTH = A_WIDTH + 2;         // holds +-2A without overflow

    // S1: register operands, precompute the 4 multiples
    wire signed [E_WIDTH-1:0] a_ext = {{(E_WIDTH-A_WIDTH){a[A_WIDTH-1]}}, a};

    reg signed [E_WIDTH-1:0] mult_p1, mult_p2, mult_n1, mult_n2;
    reg        [B_WIDTH-1:0] b_s1;

    always @(posedge clk) begin
        if (!rst_n) begin
            mult_p1 <= {E_WIDTH{1'b0}};
            mult_p2 <= {E_WIDTH{1'b0}};
            mult_n1 <= {E_WIDTH{1'b0}};
            mult_n2 <= {E_WIDTH{1'b0}};
            b_s1    <= {B_WIDTH{1'b0}};
        end else if (en) begin
            mult_p1 <=   a_ext;
            mult_p2 <=   a_ext <<< 1;
            mult_n1 <= -(a_ext);
            mult_n2 <= -(a_ext <<< 1);
            b_s1    <=   b;
        end
    end

    // S2a: recode + select partial products (combinational)
    // the appended 0 is b[-1], so group i = b_ext[2i +: 3]
    wire [B_WIDTH:0] b_ext = {b_s1, 1'b0};

    wire signed [P_WIDTH-1:0] pp [0:N_PP-1];

    genvar i;
    generate
        for (i = 0; i < N_PP; i = i + 1) begin : g_pp
            wire [2:0] grp = b_ext[2*i +: 3];

            reg signed [E_WIDTH-1:0] sel;
            always @(*) begin
                case (grp)
                    3'b001, 3'b010: sel = mult_p1;   // +1
                    3'b011:         sel = mult_p2;   // +2
                    3'b100:         sel = mult_n2;   // -2
                    3'b101, 3'b110: sel = mult_n1;   // -1
                    default:        sel = {E_WIDTH{1'b0}};
                endcase
            end

            wire signed [P_WIDTH-1:0] sel_ext =
                {{(P_WIDTH-E_WIDTH){sel[E_WIDTH-1]}}, sel};

            assign pp[i] = sel_ext <<< (2*i);
        end
    endgenerate

    // S2b/S3/S4: adder tree, one add per stage
    reg signed [P_WIDTH-1:0] lvl1_0, lvl1_1, lvl1_2, lvl1_3;   // 8 -> 4
    reg signed [P_WIDTH-1:0] lvl2_0, lvl2_1;                   // 4 -> 2
    reg signed [P_WIDTH-1:0] prod_r;                           // 2 -> 1

    always @(posedge clk) begin
        if (!rst_n) begin
            lvl1_0 <= {P_WIDTH{1'b0}};
            lvl1_1 <= {P_WIDTH{1'b0}};
            lvl1_2 <= {P_WIDTH{1'b0}};
            lvl1_3 <= {P_WIDTH{1'b0}};
            lvl2_0 <= {P_WIDTH{1'b0}};
            lvl2_1 <= {P_WIDTH{1'b0}};
            prod_r <= {P_WIDTH{1'b0}};
        end else if (en) begin
            lvl1_0 <= pp[0] + pp[1];
            lvl1_1 <= pp[2] + pp[3];
            lvl1_2 <= pp[4] + pp[5];
            lvl1_3 <= pp[6] + pp[7];

            lvl2_0 <= lvl1_0 + lvl1_1;
            lvl2_1 <= lvl1_2 + lvl1_3;

            prod_r <= lvl2_0 + lvl2_1;
        end
    end

    assign p = prod_r;

    // synthesis translate_off
    initial begin
        if (B_WIDTH != 16) begin
            $display("FATAL %m: tree is hardcoded for B_WIDTH=16 (got %0d)", B_WIDTH);
            $finish;
        end
        if (B_WIDTH % 2 != 0) begin
            $display("FATAL %m: B_WIDTH must be even (got %0d)", B_WIDTH);
            $finish;
        end
    end
    // synthesis translate_on

endmodule
