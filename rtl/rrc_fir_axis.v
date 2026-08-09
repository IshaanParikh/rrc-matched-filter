// AXI4-Stream wrapper around a dual (I/Q) RRC matched filter
//
// TDATA is one complex sample per beat, packed {Q[31:16], I[15:0]}, each
// component Q1.15. I and Q go through two identical filter cores.
//
// s_axis_tready   = m_axis_tready
// pipeline_enable = s_axis_tvalid & m_axis_tready   (beat accepted)
// pipe_advance    = m_axis_tready                   (registers move)
// m_axis_tvalid   = valid_pipe[LATENCY-1]            (registered, not comb)
//
// why two enables: if the pipeline only advances on (tvalid & tready), a beat
// already sitting on the master port stays valid while the source is idle,
// and the sink transfers it twice. advancing on tready alone fixes it - an
// idle source just pushes an invalid bubble through. the delay line still
// only moves on real samples so filter history stays clean.
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module rrc_fir_axis #(
    parameter N_TAPS      = 33,
    parameter DATA_WIDTH  = 16,
    parameter COEFF_WIDTH = 16,
    parameter COEFF_FILE  = "data/rrc_coeffs.hex"
) (
    input  wire                      clk,
    input  wire                      rst_n,          // sync, active low

    input  wire                      s_axis_tvalid,
    output wire                      s_axis_tready,
    input  wire [2*DATA_WIDTH-1:0]   s_axis_tdata,   // {Q, I}
    input  wire                      s_axis_tlast,

    output wire                      m_axis_tvalid,
    input  wire                      m_axis_tready,
    output wire [2*DATA_WIDTH-1:0]   m_axis_tdata,   // {Q, I}
    output wire                      m_axis_tlast
);

    localparam TREE_LEVELS = `RRC_CLOG2(N_TAPS);
    // must match rrc_fir_core's LATENCY, testbench asserts on this
    localparam LATENCY = 1 + `BOOTH_R4_LATENCY + TREE_LEVELS + 1;

    wire pipeline_enable = s_axis_tvalid & m_axis_tready;
    wire pipe_advance    = m_axis_tready;

    assign s_axis_tready = m_axis_tready;

    // valid/tlast shift register, LATENCY deep
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
