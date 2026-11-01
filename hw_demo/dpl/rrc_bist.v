// At-speed self-test for the board build. The USB link only delivers a
// sample every ~1 ms, so on its own it never runs the filter back to back.
// This drives one sample into the filter on every clock for 2^LEN_LOG2
// clocks, from a 32-bit LFSR, and folds every output into a CRC-32.
// python/dpl_bist.py computes the same CRC from the bit-exact model, so a
// match means every one of those outputs was right at the full clock rate.
//
//   start   : one-cycle pulse. clears the filter, then runs.
//   abort   : one-cycle pulse, stops a run (host trigger 0)
//   active  : the self-test owns the filter's din / sample_en
//   core_clr: synchronous clear for the filter, held for two cycles
//   done    : results are valid (until the next start)
//
// results:
//   crc     : CRC-32 (IEEE, reflected, same as zlib.crc32) over the outputs,
//             each one as two bytes, low byte first
//   n_out   : outputs folded in, must be 2^LEN_LOG2
//   cycles  : clocks from the first input to the last output inclusive,
//             must be 2^LEN_LOG2 + LATENCY: no gaps anywhere
//   n_sat   : outputs at either rail (the stimulus is full-scale random, so
//             a few percent of them saturate)
//
// LFSR: Galois, right shift, mask 32'h80200003 (x^32 + x^22 + x^2 + x + 1),
// shifted 16 times per sample, sample = state[31:16] after the shifts.
`timescale 1ns / 1ps

module rrc_bist #(
    parameter DATA_WIDTH = 16,
    parameter LATENCY    = 11,
    parameter LEN_LOG2   = 20,
    parameter SEED       = 32'hACE1_2025
) (
    input  wire                         clk,
    input  wire                         rst_n,
    input  wire                         start,
    input  wire                         abort,

    output wire                         active,
    output reg                          core_clr,
    output reg                          sample_en,
    output wire signed [DATA_WIDTH-1:0] din,
    input  wire signed [DATA_WIDTH-1:0] dout,

    output reg                          done,
    output reg  [31:0]                  crc,
    output reg  [31:0]                  n_out,
    output reg  [31:0]                  cycles,
    output reg  [31:0]                  n_sat
);

    localparam S_IDLE = 2'd0,
               S_CLR  = 2'd1,
               S_RUN  = 2'd2,
               S_TAIL = 2'd3;

    localparam [DATA_WIDTH-1:0] RAIL_HI = {1'b0, {(DATA_WIDTH-1){1'b1}}};
    localparam [DATA_WIDTH-1:0] RAIL_LO = {1'b1, {(DATA_WIDTH-1){1'b0}}};

    reg [1:0]        state;
    reg [1:0]        clr_cnt;
    reg [LEN_LOG2:0] n_in;
    reg [31:0]       lfsr;
    reg [LATENCY-1:0] valid_pipe;

    assign active = (state != S_IDLE);
    assign din    = lfsr[31:16];

    // 16 shifts per sample, so every sample gets 16 fresh bits. one shift
    // per sample would make each sample roughly half the one before it.
    function [31:0] lfsr_step16(input [31:0] s);
        integer k;
        reg [31:0] x;
        begin
            x = s;
            for (k = 0; k < 16; k = k + 1)
                x = x[0] ? ((x >> 1) ^ 32'h80200003) : (x >> 1);
            lfsr_step16 = x;
        end
    endfunction

    // reflected CRC-32, 16 data bits per call, low bit first. it is all
    // XORs, so synthesis flattens the loop into one level of XOR trees.
    function [31:0] crc_step16(input [31:0] c, input [15:0] d);
        integer k;
        reg [31:0] x;
        begin
            x = c;
            for (k = 0; k < 16; k = k + 1)
                x = (x[0] ^ d[k]) ? ((x >> 1) ^ 32'hEDB88320) : (x >> 1);
            crc_step16 = x;
        end
    endfunction

    wire out_valid = valid_pipe[LATENCY-1];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state      <= S_IDLE;
            clr_cnt    <= 2'd0;
            n_in       <= {(LEN_LOG2+1){1'b0}};
            lfsr       <= SEED;
            valid_pipe <= {LATENCY{1'b0}};
            core_clr   <= 1'b0;
            sample_en  <= 1'b0;
            done       <= 1'b0;
            crc        <= 32'hFFFF_FFFF;
            n_out      <= 32'd0;
            cycles     <= 32'd0;
            n_sat      <= 32'd0;
        end else if (abort) begin
            state      <= S_IDLE;
            valid_pipe <= {LATENCY{1'b0}};
            core_clr   <= 1'b0;
            sample_en  <= 1'b0;
            done       <= 1'b0;
        end else begin
            valid_pipe <= {valid_pipe[LATENCY-2:0], sample_en};

            if (out_valid) begin
                crc   <= crc_step16(crc, dout);
                n_out <= n_out + 1'b1;
                if (dout == RAIL_HI || dout == RAIL_LO)
                    n_sat <= n_sat + 1'b1;
            end
            if (sample_en || (|valid_pipe))
                cycles <= cycles + 1'b1;

            case (state)
                S_IDLE: begin
                    if (start) begin
                        done     <= 1'b0;
                        crc      <= 32'hFFFF_FFFF;
                        n_out    <= 32'd0;
                        cycles   <= 32'd0;
                        n_sat    <= 32'd0;
                        n_in     <= {(LEN_LOG2+1){1'b0}};
                        lfsr     <= SEED;
                        core_clr <= 1'b1;
                        clr_cnt  <= 2'd0;
                        state    <= S_CLR;
                    end
                end
                S_CLR: begin
                    clr_cnt <= clr_cnt + 1'b1;
                    if (clr_cnt == 2'd1) begin
                        core_clr  <= 1'b0;
                        state     <= S_RUN;
                    end
                end
                S_RUN: begin
                    // one sample per clock, no gaps
                    sample_en <= 1'b1;
                    lfsr      <= lfsr_step16(lfsr);
                    n_in      <= n_in + 1'b1;
                    if (n_in == (1 << LEN_LOG2) - 1)
                        state <= S_TAIL;
                end
                S_TAIL: begin
                    sample_en <= 1'b0;
                    // the last output lands LATENCY cycles after the last input
                    if (out_valid && !(|valid_pipe[LATENCY-2:0]) && !sample_en) begin
                        crc   <= ~crc_step16(crc, dout);
                        state <= S_IDLE;
                        done  <= 1'b1;
                    end
                end
            endcase
        end
    end

endmodule
