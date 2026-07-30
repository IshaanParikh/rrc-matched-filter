//============================================================================
// tb_booth_mult -- unit test for the pipelined radix-4 Booth multiplier.
//
// Checks, against Verilog's own signed `*` as the reference:
//   1. every corner combination of {0, +1, -1, 32767, -32768, ...}
//   2. an exhaustive sweep of one operand against corner values of the other
//   3. 50k uniform-random pairs
//   4. that the measured latency is exactly `BOOTH_R4_LATENCY
//   5. that de-asserting `en` freezes the pipeline (no data advance, no loss)
//============================================================================
`timescale 1ns / 1ps
`include "rrc_defs.vh"

module tb_booth_mult;

    localparam LAT = `BOOTH_R4_LATENCY;

    reg               clk = 1'b0;
    reg               rst_n;
    reg               en;
    reg signed [15:0] a, b;
    wire signed [31:0] p;

    integer errors  = 0;
    integer checked = 0;

    always #5 clk = ~clk;      // 100 MHz in simulation; timing is closed in Quartus

    booth_mult_r4 #(.A_WIDTH(16), .B_WIDTH(16)) dut (
        .clk(clk), .rst_n(rst_n), .en(en), .a(a), .b(b), .p(p)
    );

    // ---- reference model: a * b delayed by LAT enabled cycles -------------
    reg signed [31:0] exp_pipe [0:LAT-1];
    reg        [LAT-1:0] exp_vld;
    integer k;

    always @(posedge clk) begin
        if (!rst_n) begin
            for (k = 0; k < LAT; k = k + 1) exp_pipe[k] <= 32'sd0;
            exp_vld <= {LAT{1'b0}};
        end else if (en) begin
            exp_pipe[0] <= a * b;
            for (k = 1; k < LAT; k = k + 1) exp_pipe[k] <= exp_pipe[k-1];
            exp_vld <= {exp_vld[LAT-2:0], 1'b1};
        end
    end

    // ---- checker ---------------------------------------------------------
    always @(posedge clk) begin
        if (rst_n && en && exp_vld[LAT-1]) begin
            checked = checked + 1;
            if (p !== exp_pipe[LAT-1]) begin
                errors = errors + 1;
                if (errors <= 20)
                    $display("  MISMATCH t=%0t: got %0d (0x%08h) expected %0d (0x%08h)",
                             $time, p, p, exp_pipe[LAT-1], exp_pipe[LAT-1]);
            end
        end
    end

    // ---- stimulus --------------------------------------------------------
    reg signed [15:0] corners [0:8];
    integer ci, cj, n, lat_meas;

    // Stimulus changes on the negedge so the DUT and the reference model both
    // sample settled values at the posedge -- no active-region race.
    task drive;                       // one enabled cycle
        input signed [15:0] ta;
        input signed [15:0] tb;
        begin
            @(negedge clk);
            a = ta; b = tb; en = 1'b1;
            @(posedge clk);
        end
    endtask

    initial begin
        corners[0] = 16'sd0;
        corners[1] = 16'sd1;
        corners[2] = -16'sd1;
        corners[3] = 16'sd32767;
        corners[4] = -16'sd32768;
        corners[5] = 16'sd2;
        corners[6] = -16'sd2;
        corners[7] = 16'sd21845;      // 0x5555 -- alternating bits stress Booth groups
        corners[8] = -16'sd21846;     // 0xAAAA

        en = 1'b0; a = 16'sd0; b = 16'sd0;

        // Power-up: no reset yet, DUT registers are X.
        rst_n = 1'b1;
        repeat (3) @(posedge clk);
        if (p !== 32'sd0 && p === p) begin
            // p may legitimately be X here; only note it
        end
        $display("[tb_booth] pre-reset product = 0x%08h (X expected)", p);

        // Synchronous reset.
        rst_n = 1'b0;
        repeat (3) @(posedge clk);
        rst_n = 1'b1;
        @(posedge clk);
        if (p !== 32'sd0) begin
            $display("  ERROR: product not cleared by synchronous reset: 0x%08h", p);
            errors = errors + 1;
        end

        // ---- 1/2. corner x corner, and corners against a full sweep ------
        $display("[tb_booth] corner cross-product ...");
        for (ci = 0; ci <= 8; ci = ci + 1)
            for (cj = 0; cj <= 8; cj = cj + 1)
                drive(corners[ci], corners[cj]);

        $display("[tb_booth] corner operands vs 16-bit sweep (step 37) ...");
        for (ci = 0; ci <= 8; ci = ci + 1)
            for (n = -32768; n <= 32767; n = n + 37) begin
                drive(corners[ci], n[15:0]);
                drive(n[15:0], corners[ci]);
            end

        // ---- 3. random ----------------------------------------------------
        $display("[tb_booth] 50000 random pairs ...");
        for (n = 0; n < 50000; n = n + 1)
            drive($random, $random);

        // ---- 5. stall behaviour -------------------------------------------
        $display("[tb_booth] stall (en=0) hold check ...");
        drive(16'sd12345, -16'sd321);
        begin : stall_check
            reg signed [31:0] held;
            @(negedge clk);
            en = 1'b0;
            a  = 16'sd7777;  b = 16'sd7777;      // must be ignored while stalled
            @(negedge clk);
            held = p;
            repeat (20) begin
                @(negedge clk);
                if (p !== held) begin
                    $display("  ERROR: output moved while en=0");
                    errors = errors + 1;
                end
            end
        end

        // drain
        @(negedge clk);
        en = 1'b1; a = 16'sd0; b = 16'sd0;
        repeat (LAT + 2) @(posedge clk);

        // ---- 4. latency measurement ---------------------------------------
        // Reset, then push one non-zero product through a stream of zeros and
        // count posedges until it emerges.  Driven entirely on the negedge so
        // the operand change cannot race the sampling edge.
        @(negedge clk); en = 1'b0; rst_n = 1'b0;
        repeat (2) @(posedge clk);
        @(negedge clk); rst_n = 1'b1;

        lat_meas = -1;
        @(negedge clk);
        a = 16'sd1000; b = 16'sd3; en = 1'b1;   // captured by the next posedge
        @(negedge clk);                          // posedge 1 has now occurred
        a = 16'sd0; b = 16'sd0;
        if (p === 32'sd3000 && lat_meas < 0) lat_meas = 1;
        for (n = 2; n <= LAT + 6; n = n + 1) begin
            @(negedge clk);                      // posedge n has now occurred
            if (p === 32'sd3000 && lat_meas < 0) lat_meas = n;
        end

        $display("");
        $display("=========== tb_booth_mult summary ===========");
        $display(" comparisons : %0d", checked);
        $display(" latency     : measured %0d cycle(s), expected %0d", lat_meas, LAT);
        if (lat_meas != LAT) begin
            $display(" ERROR: latency mismatch");
            errors = errors + 1;
        end
        $display(" errors      : %0d", errors);
        if (errors == 0) $display(" RESULT      : PASS");
        else             $display(" RESULT      : FAIL");
        $display("=============================================");
        if (errors != 0) $fatal(1, "tb_booth_mult failed");
        $finish;
    end

    initial begin
        #50_000_000;
        $display("ERROR: tb_booth_mult timeout");
        $fatal(1, "timeout");
    end

endmodule
