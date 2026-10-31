// end-to-end test of hw_demo/dpl/dpl_top.v through EPT's real Active
// Transfer library: a host model speaks the library's UART framing, sends
// samples lockstep (as dpl_top.v requires), collects the filtered samples
// coming back and checks every one against a behavioural FIR computed here
// from the same coefficients.
//
// needs EPT's library sources in hw_demo/dpl/ept/src/, see the README there.
// run from repo root: ./hw_demo/dpl/sim/run_dpl_sim.sh
//
// the library's baud divider is a module parameter, so it's overridden here
// to keep the run short. the framing and every state machine are unchanged.
//
// it also runs the at-speed self-test (trigger bit 1) at a short length and
// checks the 9-word report against python/dpl_bist.py's prediction, which
// run_dpl_sim.sh writes to build/bist/bist_expected.vh.
`timescale 1ns / 1ps
`include "bist_expected.vh"

module tb_dpl_top;

    localparam N_TAPS     = 21;
    localparam COEFF_FILE = "hw_demo/data/rrc_coeffs.hex";
    localparam ADDR       = 3'h1;

    // 16x oversampling tick every UART_DIV+1 clocks. the board runs 35
    // (66 MHz / 36 / 16 = 114.6 kbaud). 7 is 4.5x faster and still leaves
    // the receiver 8 clocks per tick.
    localparam UART_DIV   = 8'd7;
    localparam BIT_CYC    = 16 * (UART_DIV + 1);

    localparam N_SAMPLES  = 48;

    reg clk = 1'b0;
    always #7.576 clk = ~clk;   // 66 MHz

    reg  rst_pin = 1'b0;
    reg  host_tx = 1'b1;        // -> UART_IN
    wire host_rx;               // <- UART_OUT
    wire [3:0] led;

    dpl_top #(
        .N_TAPS      (N_TAPS),
        .COEFF_FILE  (COEFF_FILE),
        .SAMPLE_ADDR (ADDR),
        .RESET_COUNT (12'd40),
        .HEARTBEAT_W (8),
        .BIST_LEN_LOG2 (`BIST_EXP_LEN_LOG2)
    ) dut (
        .CLK_66MHZ (clk),
        .RST       (rst_pin),
        .UART_IN   (host_tx),
        .UART_OUT  (host_rx),
        .LED       (led)
    );

    defparam dut.u_ept.ACTIVE_TRANSFER_UART_INST.COUNT_TO_DECIMAL_2 = UART_DIV;

    //------------------------------------------------------------------
    // reference model: same integer maths as python/golden_model.py
    //------------------------------------------------------------------
    reg signed [15:0] h [0:N_TAPS-1];
    reg signed [15:0] hist [0:N_TAPS-1];
    reg signed [15:0] expected [0:255];
    integer n_expected = 0;

    task clear_history;
        integer k;
        begin
            for (k = 0; k < N_TAPS; k = k + 1) hist[k] = 16'sd0;
        end
    endtask

    function signed [15:0] push_and_filter(input signed [15:0] x);
        integer k;
        reg signed [63:0] acc;
        begin
            for (k = N_TAPS - 1; k > 0; k = k - 1) hist[k] = hist[k-1];
            hist[0] = x;
            acc = 0;
            for (k = 0; k < N_TAPS; k = k + 1)
                acc = acc + hist[k] * h[k];
            acc = (acc + 64'sd16384) >>> 15;
            if (acc > 64'sd32767)       push_and_filter = 16'sh7fff;
            else if (acc < -64'sd32768) push_and_filter = 16'sh8000;
            else                        push_and_filter = acc[15:0];
        end
    endfunction

    //------------------------------------------------------------------
    // host side of the EPT framing: {2'b10, cmd, addr} then the payload
    //------------------------------------------------------------------
    task uart_send(input [7:0] b);
        integer i;
        begin
            host_tx = 1'b0;
            repeat (BIT_CYC) @(posedge clk);
            for (i = 0; i < 8; i = i + 1) begin
                host_tx = b[i];
                repeat (BIT_CYC) @(posedge clk);
            end
            host_tx = 1'b1;
            repeat (BIT_CYC) @(posedge clk);
        end
    endtask

    task send_transfer(input [7:0] payload);
        begin
            uart_send({2'b10, 3'h2, ADDR});
            uart_send(payload);
        end
    endtask

    task send_trigger(input [7:0] bits);
        begin
            uart_send({2'b10, 3'h1, 3'h0});
            uart_send(bits);
        end
    endtask

    task wait_outputs;
        integer guard;
        begin
            guard = 0;
            while (n_got < n_expected && guard < 400 * BIT_CYC * 10) begin
                @(posedge clk);
                guard = guard + 1;
            end
        end
    endtask

    task expect_word(input [15:0] w);
        begin
            expected[n_expected] = w;
            n_expected = n_expected + 1;
        end
    endtask

    task send_sample(input signed [15:0] x);
        begin
            expected[n_expected] = push_and_filter(x);
            n_expected = n_expected + 1;
            send_transfer(x[7:0]);
            send_transfer(x[15:8]);
            wait_outputs;
        end
    endtask

    //------------------------------------------------------------------
    // host receiver: bytes -> frames -> 16-bit samples, checked in order
    //------------------------------------------------------------------
    reg [7:0] rx_b;
    integer   rx_bytes = 0;
    integer   n_got = 0;
    integer   errors = 0;
    reg [7:0] frame_hdr;
    reg [7:0] lo;
    reg       have_lo = 1'b0;
    reg signed [15:0] got;

    task uart_recv(output [7:0] b);
        integer i;
        begin
            @(negedge host_rx);
            repeat (BIT_CYC + BIT_CYC/2) @(posedge clk);
            for (i = 0; i < 8; i = i + 1) begin
                b[i] = host_rx;
                repeat (BIT_CYC) @(posedge clk);
            end
            if (host_rx !== 1'b1) begin
                $display("FAIL: framing error on UART_OUT at %0t", $time);
                errors = errors + 1;
            end
        end
    endtask

    always begin
        uart_recv(frame_hdr);
        uart_recv(rx_b);
        rx_bytes = rx_bytes + 2;
        if (frame_hdr !== {2'b11, 3'h2, ADDR}) begin
            $display("FAIL: unexpected frame header %02h", frame_hdr);
            errors = errors + 1;
        end else if (!have_lo) begin
            lo      = rx_b;
            have_lo = 1'b1;
        end else begin
            got     = {rx_b, lo};
            have_lo = 1'b0;
            if (n_got >= n_expected) begin
                $display("FAIL: extra output sample %0d = %0d", n_got, got);
                errors = errors + 1;
            end else if (got !== expected[n_got]) begin
                $display("FAIL: sample %0d got %0d expected %0d", n_got, got, expected[n_got]);
                errors = errors + 1;
            end
            n_got = n_got + 1;
        end
    end

    //------------------------------------------------------------------
    // stimulus
    //------------------------------------------------------------------
    integer i, seed;
    reg signed [15:0] x;


    initial begin
        $readmemh(COEFF_FILE, h);
        clear_history;
        seed = 32'h5eed;

        repeat (20) @(posedge clk);
        rst_pin = 1'b1;
        repeat (200) @(posedge clk);

        // impulse: the output should be the taps themselves
        send_sample(16'sh7fff);
        for (i = 1; i < N_TAPS + 2; i = i + 1)
            send_sample(16'sd0);

        // random, rails included so saturation is exercised
        for (i = 0; i < N_SAMPLES - (N_TAPS + 2); i = i + 1) begin
            x = $random(seed);
            if (i % 7 == 0) x = 16'sh7fff;
            if (i % 11 == 0) x = 16'sh8000;
            send_sample(x);
        end

        // host trigger 0 clears the history: the next impulse must look
        // exactly like the first one
        send_trigger(8'h01);
        repeat (4 * BIT_CYC) @(posedge clk);
        clear_history;
        send_sample(16'sh4000);
        for (i = 1; i < 6; i = i + 1)
            send_sample(16'sd0);

        // at-speed self-test: 9 report words come back like filter outputs
        expect_word(16'hB157);
        expect_word(`BIST_EXP_CRC & 16'hFFFF);    expect_word(`BIST_EXP_CRC >> 16);
        expect_word(`BIST_EXP_N_OUT & 16'hFFFF);  expect_word(`BIST_EXP_N_OUT >> 16);
        expect_word(`BIST_EXP_CYCLES & 16'hFFFF); expect_word(`BIST_EXP_CYCLES >> 16);
        expect_word(`BIST_EXP_N_SAT & 16'hFFFF);  expect_word(`BIST_EXP_N_SAT >> 16);
        send_trigger(8'h02);
        wait_outputs;

        // and the host path still works afterwards, from a clean state
        send_trigger(8'h01);
        repeat (4 * BIT_CYC) @(posedge clk);
        clear_history;
        send_sample(16'sh7fff);
        for (i = 1; i < 4; i = i + 1)
            send_sample(16'sd0);

        if (n_got != n_expected) begin
            $display("FAIL: got %0d of %0d output samples", n_got, n_expected);
            errors = errors + 1;
        end
        if (dut.overflow) begin
            $display("FAIL: dpl_top dropped a result (overflow)");
            errors = errors + 1;
        end

        $display("");
        $display("dpl_top through the EPT library:");
        $display("  samples in/out : %0d / %0d", n_expected, n_got);
        $display("  UART bytes back: %0d", rx_bytes);
        $display("  mismatches     : %0d", errors);
        if (errors == 0) $display("PASS");
        else             $display("FAIL");
        $finish;
    end

    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("build/tb_dpl_top.vcd");
            $dumpvars(0, tb_dpl_top);
        end
    end

endmodule
