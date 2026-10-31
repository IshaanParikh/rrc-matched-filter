// Board top level for the EPT DueProLogic (EPT-4CE6-AF-D2, USB-C rev,
// EP4CE6E22C8N). One rrc_fir_core channel, fed and drained over USB by
// EPT's Active Transfer library.
//
//   PC --USB--> FT2232H --UART 115200--> active_serial_library
//      --> active_transfer @ SAMPLE_ADDR --> byte pairs -> Q1.15 sample
//      --> rrc_fir_core --> active_transfer --> back to PC
//
// wire protocol (all through the one active_transfer endterm, LSB first):
//   host -> fpga : x[n][7:0], x[n][15:8]     second byte loads the sample
//   fpga -> host : y[n][7:0], y[n][15:8]     one pair per input sample
//   host trigger bit 0 : clear the filter history and the byte phase, so a
//                        run starts from the same zero state as
//                        python/golden_model.py
//   host trigger bit 1 : at-speed self-test (rrc_bist.v). the filter takes
//                        an LFSR sample on every clock for 2^BIST_LEN_LOG2
//                        clocks, then 9 report words come back the same way
//                        filter outputs do. python/dpl_bist.py checks them.
//
// on the wire each active transfer is two UART bytes, {2'b10|2'b11, cmd,
// addr} then the payload, so one sample costs 4 bytes each way.
//
// the host must run lockstep: send x[n], wait for y[n], then send x[n+1].
// inside the library the UART receiver and the FT245-style write path share
// one tri-state byte bus, and a byte arriving while a frame is going out
// drives it from both ends (X in simulation, a garbled byte on silicon).
// lockstep never overlaps the two directions. python/dpl_host.py does this.
//
// the library is EPT's code and isn't in this repo, see hw_demo/dpl/ept/.
// port names follow EPT's own EPT_4CE6_AF_D1_Top so their pinout applies.
`timescale 1ns / 1ps
`default_nettype wire
`include "rrc_defs.vh"

module dpl_top #(
    parameter N_TAPS       = 21,
    parameter DATA_WIDTH   = 16,
    parameter COEFF_FILE   = "hw_demo/data/rrc_coeffs.hex",
    parameter SAMPLE_ADDR  = 3'h1,
    parameter RESET_COUNT  = 12'h9c8,    // power-on reset, same as EPT's demo
    parameter HEARTBEAT_W  = 26,         // 66 MHz / 2^26 ~ 1 Hz blink
    parameter BUSY_TIMEOUT = 20'hfffff,  // ~16 ms, escape hatch if the library never answers
    parameter BIST_LEN_LOG2 = 20         // self-test length, 2^20 samples = 16 ms at 66 MHz
) (
    input  wire       CLK_66MHZ,         // PIN_23, 66 MHz osc on GCLK1 (EPT calls it aa[1])
    input  wire       RST,               // PIN_89, pulled up to 3V3 on the board, low = reset

    input  wire       UART_IN,           // PIN_43, FT2232H BDBUS0 (TXD) -> FPGA
    output wire       UART_OUT,          // PIN_38, FPGA -> FT2232H BDBUS1 (RXD)

    output wire [3:0] LED                // XIO_6[3:0], LED0..3 of the 6x6 array, active low
);

    localparam TREE_LEVELS = `RRC_CLOG2(N_TAPS);
    // must match rrc_fir_core's LATENCY
    localparam LATENCY = 1 + `BOOTH_R4_LATENCY + TREE_LEVELS + 1;

    // the library holds start_transfer for its own 3-cycle window, give it 4
    localparam START_HOLD = 3'd4;

    //------------------------------------------------------------------
    // reset: counter released by RST, exactly as EPT's demo does it.
    // rst_n drives both the library (aa[0]) and our logic.
    //------------------------------------------------------------------
    reg  [11:0] reset_counter;
    reg         rst_n;

    always @(posedge CLK_66MHZ or negedge RST) begin
        if (!RST) begin
            reset_counter <= 12'd0;
            rst_n         <= 1'b0;
        end else if (reset_counter < RESET_COUNT) begin
            reset_counter <= reset_counter + 1'b1;
            rst_n         <= 1'b0;
        end else begin
            rst_n         <= 1'b1;
        end
    end

    // UART_IN is asynchronous to CLK_66MHZ. idle line is high.
    reg [2:0] uart_sync;
    always @(posedge CLK_66MHZ or negedge rst_n) begin
        if (!rst_n) uart_sync <= 3'b111;
        else        uart_sync <= {uart_sync[1:0], UART_IN};
    end

    //------------------------------------------------------------------
    // EPT Active Transfer library + endterms
    //------------------------------------------------------------------
    wire [1:0]  aa = {CLK_66MHZ, rst_n};
    wire [23:0] UC_IN;
    wire [21:0] UC_OUT;
    wire [2*22-1:0] uc_out_m;

    active_serial_library u_ept (
        .aa        (aa),
        .UART_OUT  (UART_OUT),
        .UART_IN   (uart_sync[2]),
        .UC_IN     (UC_IN),
        .UC_OUT    (UC_OUT),
        .STATE_OUT (),
        .TEST_OUT  ()
    );

    eptWireOR #(.N(2)) u_uc_or (UC_OUT, uc_out_m);

    wire [7:0] trigger_from_host;

    active_trigger u_trigger (
        .uc_clk            (CLK_66MHZ),
        .uc_reset          (rst_n),
        .uc_in             (UC_IN),
        .uc_out            (uc_out_m[0*22 +: 22]),
        .trigger_to_host   (8'h00),
        .trigger_to_device (trigger_from_host)
    );

    reg        tx_start;
    reg  [7:0] tx_byte;
    wire       rx_received;
    wire [7:0] rx_byte;

    active_transfer u_samples (
        .uc_clk             (CLK_66MHZ),
        .uc_reset           (rst_n),
        .uc_in              (UC_IN),
        .uc_out             (uc_out_m[1*22 +: 22]),
        .start_transfer     (tx_start),
        .transfer_received  (rx_received),
        // the library's transfer_busy also latches on every *received*
        // byte and only clears after the next send, so it can't gate
        // sends. the endpoint busy bit on UC_IN is used instead.
        .transfer_busy      (),
        .uc_addr            (SAMPLE_ADDR),
        .transfer_to_host   (tx_byte),
        .transfer_to_device (rx_byte)
    );

    // UC_IN[23]: the library's endpoint is busy writing a frame to the host
    wire ept_busy = UC_IN[23];

    //------------------------------------------------------------------
    // host trigger bit 0 -> clear. trigger_to_device is combinational off
    // UC_IN and only lasts a few cycles, so edge-detect it.
    //------------------------------------------------------------------
    reg trig0_d, trig1_d;
    reg clear;
    reg bist_start;
    always @(posedge CLK_66MHZ or negedge rst_n) begin
        if (!rst_n) begin
            trig0_d    <= 1'b0;
            trig1_d    <= 1'b0;
            clear      <= 1'b0;
            bist_start <= 1'b0;
        end else begin
            trig0_d    <= trigger_from_host[0];
            trig1_d    <= trigger_from_host[1];
            clear      <= trigger_from_host[0] & ~trig0_d;
            bist_start <= trigger_from_host[1] & ~trig1_d;
        end
    end

    //------------------------------------------------------------------
    // byte pairs -> samples. transfer_received is high for two cycles.
    //------------------------------------------------------------------
    reg                         rx_received_d;
    reg                         have_lo;
    reg  [7:0]                  lo_byte;
    reg                         sample_en;
    reg  signed [DATA_WIDTH-1:0] sample;

    wire rx_strobe = rx_received & ~rx_received_d;

    always @(posedge CLK_66MHZ or negedge rst_n) begin
        if (!rst_n) begin
            rx_received_d <= 1'b0;
            have_lo       <= 1'b0;
            lo_byte       <= 8'h00;
            sample_en     <= 1'b0;
            sample        <= {DATA_WIDTH{1'b0}};
        end else begin
            rx_received_d <= rx_received;
            sample_en     <= 1'b0;
            if (clear) begin
                have_lo <= 1'b0;
            end else if (rx_strobe) begin
                if (!have_lo) begin
                    lo_byte <= rx_byte;
                    have_lo <= 1'b1;
                end else begin
                    sample    <= {rx_byte, lo_byte};
                    sample_en <= 1'b1;
                    have_lo   <= 1'b0;
                end
            end
        end
    end

    //------------------------------------------------------------------
    // at-speed self-test. while it runs it owns the filter's input and
    // samples from the host are ignored.
    //------------------------------------------------------------------
    wire                         bist_active, bist_clr, bist_en, bist_done;
    wire signed [DATA_WIDTH-1:0] bist_din;
    wire [31:0]                  bist_crc, bist_n_out, bist_cycles, bist_n_sat;
    wire signed [DATA_WIDTH-1:0] dout;

    rrc_bist #(
        .DATA_WIDTH (DATA_WIDTH),
        .LATENCY    (LATENCY),
        .LEN_LOG2   (BIST_LEN_LOG2)
    ) u_bist (
        .clk       (CLK_66MHZ),
        .rst_n     (rst_n),
        .start     (bist_start),
        .abort     (clear),
        .active    (bist_active),
        .core_clr  (bist_clr),
        .sample_en (bist_en),
        .din       (bist_din),
        .dout      (dout),
        .done      (bist_done),
        .crc       (bist_crc),
        .n_out     (bist_n_out),
        .cycles    (bist_cycles),
        .n_sat     (bist_n_sat)
    );

    wire                         host_en   = sample_en & ~bist_active;
    wire                         core_en   = bist_active ? bist_en  : sample_en;
    wire signed [DATA_WIDTH-1:0] core_din  = bist_active ? bist_din : sample;

    //------------------------------------------------------------------
    // the filter. pipe_en stays high: host samples arrive thousands of
    // cycles apart, the delay line only moves on sample_en, and dout
    // settles LATENCY cycles after each one and then holds.
    //------------------------------------------------------------------
    reg  filt_rst_n;
    always @(posedge CLK_66MHZ) filt_rst_n <= rst_n & ~clear & ~bist_clr;

    rrc_fir_core #(
        .N_TAPS      (N_TAPS),
        .DATA_WIDTH  (DATA_WIDTH),
        .COEFF_WIDTH (16),
        .COEFF_FILE  (COEFF_FILE)
    ) u_core (
        .clk       (CLK_66MHZ),
        .rst_n     (filt_rst_n),
        .sample_en (core_en),
        .pipe_en   (1'b1),
        .din       (core_din),
        .dout      (dout)
    );

    reg [LATENCY-1:0] valid_pipe;
    always @(posedge CLK_66MHZ or negedge rst_n) begin
        if (!rst_n)     valid_pipe <= {LATENCY{1'b0}};
        else if (clear) valid_pipe <= {LATENCY{1'b0}};
        else            valid_pipe <= {valid_pipe[LATENCY-2:0], host_en};
    end
    wire out_valid = valid_pipe[LATENCY-1];

    //------------------------------------------------------------------
    // self-test report: when a run finishes, queue 9 words into the output
    // buffer one at a time: 0xB157, then crc, n_out, cycles, n_sat as
    // low/high halves
    //------------------------------------------------------------------
    reg        bist_done_d;
    reg        rpt_active;
    reg  [3:0] rpt_idx;
    reg  [DATA_WIDTH-1:0] rpt_word;
    wire       rpt_load;

    always @(*) begin
        case (rpt_idx)
            4'd0:    rpt_word = 16'hB157;
            4'd1:    rpt_word = bist_crc[15:0];
            4'd2:    rpt_word = bist_crc[31:16];
            4'd3:    rpt_word = bist_n_out[15:0];
            4'd4:    rpt_word = bist_n_out[31:16];
            4'd5:    rpt_word = bist_cycles[15:0];
            4'd6:    rpt_word = bist_cycles[31:16];
            4'd7:    rpt_word = bist_n_sat[15:0];
            default: rpt_word = bist_n_sat[31:16];
        endcase
    end

    always @(posedge CLK_66MHZ or negedge rst_n) begin
        if (!rst_n) begin
            bist_done_d <= 1'b0;
            rpt_active  <= 1'b0;
            rpt_idx     <= 4'd0;
        end else begin
            bist_done_d <= bist_done;
            if (clear) begin
                rpt_active <= 1'b0;
            end else if (bist_done && !bist_done_d) begin
                rpt_active <= 1'b1;
                rpt_idx    <= 4'd0;
            end else if (rpt_load) begin
                if (rpt_idx == 4'd8) rpt_active <= 1'b0;
                rpt_idx <= rpt_idx + 1'b1;
            end
        end
    end

    //------------------------------------------------------------------
    // one-deep output buffer. with a lockstep host the previous result is
    // always gone before the next sample lands; if it isn't, the new one
    // is dropped and LED3 latches.
    //------------------------------------------------------------------
    reg  [DATA_WIDTH-1:0] out_word;
    reg                   out_pending;
    reg                   out_taken;
    reg                   overflow;

    always @(posedge CLK_66MHZ or negedge rst_n) begin
        if (!rst_n) begin
            out_word    <= {DATA_WIDTH{1'b0}};
            out_pending <= 1'b0;
            overflow    <= 1'b0;
        end else if (clear) begin
            out_pending <= 1'b0;
            overflow    <= 1'b0;
        end else if (out_valid) begin
            if (out_pending && !out_taken) begin
                overflow <= 1'b1;
            end else begin
                out_word    <= dout;
                out_pending <= 1'b1;
            end
        end else if (out_taken) begin
            out_pending <= 1'b0;
        end else if (rpt_load) begin
            out_word    <= rpt_word;
            out_pending <= 1'b1;
        end
    end

    assign rpt_load = rpt_active && !out_pending && !out_taken && !out_valid && !clear;

    //------------------------------------------------------------------
    // result -> host. per byte: hold start_transfer for START_HOLD cycles,
    // then wait for the endpoint to go busy and idle again, which is when
    // the frame has been handed to the UART. a second start while the
    // endpoint is busy would be dropped silently, hence the wait.
    //------------------------------------------------------------------
    localparam TX_IDLE  = 3'd0,
               TX_START = 3'd2,
               TX_BUSY  = 3'd3,
               TX_DONE  = 3'd4;

    reg [2:0]  tx_state;
    reg        tx_hi;               // 0: sending low byte, 1: high byte
    reg [2:0]  hold_cnt;
    reg        seen_busy;
    reg [19:0] busy_timer;
    reg [DATA_WIDTH-1:0] tx_word;

    always @(posedge CLK_66MHZ or negedge rst_n) begin
        if (!rst_n) begin
            tx_state   <= TX_IDLE;
            tx_hi      <= 1'b0;
            hold_cnt   <= 3'd0;
            seen_busy  <= 1'b0;
            busy_timer <= 20'd0;
            tx_word    <= {DATA_WIDTH{1'b0}};
            tx_start   <= 1'b0;
            tx_byte    <= 8'h00;
            out_taken  <= 1'b0;
        end else begin
            out_taken <= 1'b0;
            case (tx_state)
                TX_IDLE: begin
                    tx_start <= 1'b0;
                    if (out_pending && !out_taken && !clear) begin
                        tx_word   <= out_word;
                        tx_byte   <= out_word[7:0];
                        tx_hi     <= 1'b0;
                        out_taken <= 1'b1;
                        hold_cnt  <= 3'd0;
                        tx_state  <= TX_START;
                    end
                end
                TX_START: begin
                    tx_start <= 1'b1;
                    hold_cnt <= hold_cnt + 1'b1;
                    if (hold_cnt == START_HOLD - 1) begin
                        seen_busy  <= 1'b0;
                        busy_timer <= 20'd0;
                        tx_state   <= TX_BUSY;
                    end
                end
                TX_BUSY: begin
                    tx_start   <= 1'b0;
                    busy_timer <= busy_timer + 1'b1;
                    if (ept_busy)
                        seen_busy <= 1'b1;
                    if ((seen_busy && !ept_busy) || busy_timer == BUSY_TIMEOUT)
                        tx_state <= TX_DONE;
                end
                TX_DONE: begin
                    if (!tx_hi) begin
                        tx_hi    <= 1'b1;
                        tx_byte  <= tx_word[15:8];
                        hold_cnt <= 3'd0;
                        tx_state <= TX_START;
                    end else begin
                        tx_state <= TX_IDLE;
                    end
                end
                default: tx_state <= TX_IDLE;
            endcase
        end
    end

    //------------------------------------------------------------------
    // LEDs, active low (the array is sunk by the FPGA through 220R to 3V3)
    //   LED0 heartbeat: clock running and out of reset
    //   LED1 toggles per input sample
    //   LED2 toggles per output sample sent
    //   LED3 a result was dropped since the last clear (host didn't wait
    //        for the reply before sending the next sample)
    //------------------------------------------------------------------
    reg [HEARTBEAT_W-1:0] heartbeat;
    reg                   in_toggle, out_toggle;

    always @(posedge CLK_66MHZ or negedge rst_n) begin
        if (!rst_n) begin
            heartbeat  <= {HEARTBEAT_W{1'b0}};
            in_toggle  <= 1'b0;
            out_toggle <= 1'b0;
        end else begin
            heartbeat <= heartbeat + 1'b1;
            if (host_en)   in_toggle  <= ~in_toggle;
            if (out_taken) out_toggle <= ~out_toggle;
        end
    end

    assign LED = ~{overflow, out_toggle, in_toggle, heartbeat[HEARTBEAT_W-1]};

endmodule
