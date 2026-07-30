//============================================================================
// booth_mult_r4 -- pipelined signed radix-4 (modified Booth) multiplier.
//
// 16 x 16 -> 32 signed, four register stages, one adder delay per stage so the
// structure closes timing at an aggressive clock.
//
// Radix-4 Booth recoding rewrites the multiplier B as
//
//     B = sum(i=0..N_PP-1)  d_i * 4^i ,   d_i = -2*b[2i+1] + b[2i] + b[2i-1]
//
// with b[-1] = 0 and d_i in {-2,-1,0,+1,+2}.  The identity is exact for a
// two's-complement B of even width, so B_WIDTH/2 = 8 partial products replace
// the 16 an unrecoded array would need -- half the compression depth.
//
// Because every d_i*A term is one of only five values, the four non-zero
// multiples are computed ONCE in S1 and shared by all eight partial-product
// multiplexers.  That keeps S2 down to "one 5:1 mux + one add" instead of
// "negate + add", which is what actually buys the Fmax.
//
// Note on width: each partial product is sign-extended to P_WIDTH and then
// shifted left, discarding bits above P_WIDTH.  The true product always fits
// in P_WIDTH bits, so the mod-2^P_WIDTH sum is exact -- the discarded bits
// cancel.  This is the standard argument that lets Booth trees run at the
// product width rather than at the (wider) partial-product width.
//
// When B is a hardwired coefficient (the RRC case) synthesis constant-folds
// the recoder and every mux, collapsing this to a hardwired shift/add network.
// Driving `b` from a writable RAM instead keeps the taps runtime-reloadable at
// the cost of that folding.
//============================================================================
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module booth_mult_r4 #(
    parameter A_WIDTH = 16,     // multiplicand (data sample)
    parameter B_WIDTH = 16      // multiplier   (filter coefficient), must be even
) (
    input  wire                              clk,
    input  wire                              rst_n,    // synchronous, active low
    input  wire                              en,       // pipeline advance
    input  wire signed [A_WIDTH-1:0]         a,
    input  wire signed [B_WIDTH-1:0]         b,
    output wire signed [A_WIDTH+B_WIDTH-1:0] p
);

    localparam P_WIDTH = A_WIDTH + B_WIDTH;   // 32
    localparam N_PP    = B_WIDTH / 2;         // 8 partial products
    localparam E_WIDTH = A_WIDTH + 2;         // 18: holds +/-2A without overflow

    // ---------------------------------------------------------------------
    // S1: register the operands and pre-compute the four non-zero multiples.
    // Sharing these across all eight PP muxes is what keeps S2 short.
    // ---------------------------------------------------------------------
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
            mult_p1 <=   a_ext;            // +1 * A
            mult_p2 <=   a_ext <<< 1;      // +2 * A
            mult_n1 <= -(a_ext);           // -1 * A
            mult_n2 <= -(a_ext <<< 1);     // -2 * A
            b_s1    <=   b;
        end
    end

    // ---------------------------------------------------------------------
    // S2a: Booth recode + partial-product select (combinational).
    // b_ext[0] supplies the implicit b[-1] = 0, so group i is b_ext[2i +: 3].
    // ---------------------------------------------------------------------
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
                    default:        sel = {E_WIDTH{1'b0}};   // 3'b000, 3'b111
                endcase
            end

            wire signed [P_WIDTH-1:0] sel_ext =
                {{(P_WIDTH-E_WIDTH){sel[E_WIDTH-1]}}, sel};

            assign pp[i] = sel_ext <<< (2*i);
        end
    endgenerate

    // ---------------------------------------------------------------------
    // S2b/S3/S4: balanced compression tree, one adder per pipeline stage.
    // Written out explicitly for N_PP == 8 (B_WIDTH == 16); the pattern
    // generalises by adding a level per doubling of B_WIDTH.
    // ---------------------------------------------------------------------
    reg signed [P_WIDTH-1:0] lvl1_0, lvl1_1, lvl1_2, lvl1_3;   // S2: 8 -> 4
    reg signed [P_WIDTH-1:0] lvl2_0, lvl2_1;                   // S3: 4 -> 2
    reg signed [P_WIDTH-1:0] prod_r;                           // S4: 2 -> 1

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

    // ---------------------------------------------------------------------
    // Elaboration-time guards (simulation only).
    // ---------------------------------------------------------------------
    // synthesis translate_off
    initial begin
        if (B_WIDTH != 16) begin
            $display("FATAL %m: booth_mult_r4 compression tree is written for B_WIDTH=16 (got %0d)", B_WIDTH);
            $finish;
        end
        if (B_WIDTH % 2 != 0) begin
            $display("FATAL %m: radix-4 Booth requires an even B_WIDTH (got %0d)", B_WIDTH);
            $finish;
        end
    end
    // synthesis translate_on

endmodule
