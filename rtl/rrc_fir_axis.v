//============================================================================
// rrc_fir_axis -- AXI4-Stream wrapper around a dual (I/Q) RRC matched filter.
//
// TDATA carries one complex sample per beat, packed {Q[31:16], I[15:0]}, each
// component Q1.15.  I and Q run through independent, identical filter cores.
//
// Flow control
// ------------
//   s_axis_tready  = m_axis_tready                  (combinational, and legal:
//                    AXI4-Stream forbids TVALID depending on TREADY, not TREADY
//                    depending on TREADY)
//   pipeline_enable = s_axis_tvalid & m_axis_tready  -- a beat is accepted
//   pipe_advance    = m_axis_tready                  -- pipeline registers move
//   m_axis_tvalid   = valid_pipe[LATENCY-1]          -- a register, never
//                                                       combinational on TREADY
//
// Why the two enables differ.  Using (tvalid & tready) for everything is the
// obvious move and it is subtly wrong: with the pipeline frozen whenever the
// source goes idle, a beat already presented on the master port stays valid
// while the sink keeps TREADY high, and the same sample is transferred twice.
// Advancing the pipeline on TREADY alone fixes that -- an idle source injects
// an invalid bubble that pushes the finished beat out exactly once -- while the
// delay line still moves only on real samples, so the filter history stays
// clean.  Zero data loss in both directions:
//
//   sink stalls   (m_tready = 0) -> everything freezes, s_tready deasserts,
//                                   TDATA/TVALID hold. Nothing is dropped.
//   source stalls (s_tvalid = 0) -> bubbles flow, in-flight results drain,
//                                   the delay line holds its history.
//
// TLAST is carried alongside TVALID through the same shift register so packet
// framing survives the filter delay.
//
// The one combinational path out of this block is TREADY.  If back-pressure
// timing ever becomes the critical path, drop a standard skid buffer on the
// master port; nothing else here needs to change.
//============================================================================
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module rrc_fir_axis #(
    parameter N_TAPS      = 33,
    parameter DATA_WIDTH  = 16,               // per I/Q component
    parameter COEFF_WIDTH = 16,
    parameter COEFF_FILE  = "data/rrc_coeffs.hex"
) (
    input  wire                      clk,
    input  wire                      rst_n,          // synchronous, active low

    // ---- AXI4-Stream slave (noisy samples in) ----
    input  wire                      s_axis_tvalid,
    output wire                      s_axis_tready,
    input  wire [2*DATA_WIDTH-1:0]   s_axis_tdata,   // {Q, I}
    input  wire                      s_axis_tlast,

    // ---- AXI4-Stream master (matched-filtered samples out) ----
    output wire                      m_axis_tvalid,
    input  wire                      m_axis_tready,
    output wire [2*DATA_WIDTH-1:0]   m_axis_tdata,   // {Q, I}
    output wire                      m_axis_tlast
);

    localparam TREE_LEVELS = `RRC_CLOG2(N_TAPS);
    // Must match rrc_fir_core's LATENCY; the testbench measures the real
    // end-to-end delay and asserts it equals this.
    localparam LATENCY = 1 + `BOOTH_R4_LATENCY + TREE_LEVELS + 1;

    // ---------------------------------------------------------------------
    // Handshake
    // ---------------------------------------------------------------------
    wire pipeline_enable = s_axis_tvalid & m_axis_tready;   // beat accepted
    wire pipe_advance    = m_axis_tready;                   // stages advance

    assign s_axis_tready = m_axis_tready;

    // ---------------------------------------------------------------------
    // Valid / TLAST shift register, LATENCY deep
    // ---------------------------------------------------------------------
    reg [LATENCY-1:0] valid_pipe;
    reg [LATENCY-1:0] last_pipe;

    always @(posedge clk) begin
        if (!rst_n) begin
            valid_pipe <= {LATENCY{1'b0}};
            last_pipe  <= {LATENCY{1'b0}};
        end else if (pipe_advance) begin
            valid_pipe <= {valid_pipe[LATENCY-2:0], pipeline_enable};
            last_pipe  <= {last_pipe[LATENCY-2:0],  pipeline_enable & s_axis_tlast};
        end
    end

    assign m_axis_tvalid = valid_pipe[LATENCY-1];
    assign m_axis_tlast  = last_pipe[LATENCY-1];

    // ---------------------------------------------------------------------
    // Dual filter cores
    // ---------------------------------------------------------------------
    wire signed [DATA_WIDTH-1:0] i_in  = s_axis_tdata[DATA_WIDTH-1:0];
    wire signed [DATA_WIDTH-1:0] q_in  = s_axis_tdata[2*DATA_WIDTH-1:DATA_WIDTH];
    wire signed [DATA_WIDTH-1:0] i_out;
    wire signed [DATA_WIDTH-1:0] q_out;

    rrc_fir_core #(
        .N_TAPS      (N_TAPS),
        .DATA_WIDTH  (DATA_WIDTH),
        .COEFF_WIDTH (COEFF_WIDTH),
        .COEFF_FILE  (COEFF_FILE)
    ) u_fir_i (
        .clk       (clk),
        .rst_n     (rst_n),
        .sample_en (pipeline_enable),
        .pipe_en   (pipe_advance),
        .din       (i_in),
        .dout      (i_out)
    );

    rrc_fir_core #(
        .N_TAPS      (N_TAPS),
        .DATA_WIDTH  (DATA_WIDTH),
        .COEFF_WIDTH (COEFF_WIDTH),
        .COEFF_FILE  (COEFF_FILE)
    ) u_fir_q (
        .clk       (clk),
        .rst_n     (rst_n),
        .sample_en (pipeline_enable),
        .pipe_en   (pipe_advance),
        .din       (q_in),
        .dout      (q_out)
    );

    assign m_axis_tdata = {q_out, i_out};

endmodule
