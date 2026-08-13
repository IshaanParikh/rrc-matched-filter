// Minimal single-channel top level for hardware bring-up on the EP4CE6E22C8N.
// Wraps one rrc_fir_core instance directly, no AXI4-Stream, no Q channel.
// Scoped down from the full 33-tap dual-channel design (rtl/rrc_fir_axis.v),
// which needs about 14,000 logic cells and does not fit the EP4CE6's 6,272.
// This is a smaller filter (fewer taps, one channel), not the full design.
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module top_single_channel #(
    parameter N_TAPS     = 21,
    parameter DATA_WIDTH = 16,
    parameter COEFF_FILE = "hw_demo/data/rrc_coeffs.hex"
) (
    input  wire                         clk,
    input  wire                         rst_n,
    input  wire signed [DATA_WIDTH-1:0] din,
    output wire signed [DATA_WIDTH-1:0] dout
);

    rrc_fir_core #(
        .N_TAPS      (N_TAPS),
        .DATA_WIDTH  (DATA_WIDTH),
        .COEFF_WIDTH (16),
        .COEFF_FILE  (COEFF_FILE)
    ) u_core (
        .clk       (clk),
        .rst_n     (rst_n),
        .sample_en (1'b1),
        .pipe_en   (1'b1),
        .din       (din),
        .dout      (dout)
    );

endmodule
