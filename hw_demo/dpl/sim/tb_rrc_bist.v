// rrc_bist + rrc_fir_core wired the way dpl_top.v wires them, checked
// against the report python/dpl_bist.py predicts. no EPT library needed.
// run from repo root: ./hw_demo/dpl/sim/run_bist_sim.sh
`timescale 1ns / 1ps
`include "rrc_defs.vh"
`include "bist_expected.vh"

module tb_rrc_bist;

    localparam N_TAPS   = 21;
    localparam LATENCY  = 1 + `BOOTH_R4_LATENCY + `RRC_CLOG2(N_TAPS) + 1;
    localparam LEN_LOG2 = `BIST_EXP_LEN_LOG2;

    reg clk = 1'b0;
    always #7.5 clk = ~clk;                  // 66 MHz-ish

    reg rst_n = 1'b0;
    reg start = 1'b0;
    reg abort = 1'b0;

    wire        active, core_clr, sample_en, done;
    wire signed [15:0] din, dout;
    wire [31:0] crc, n_out, cycles, n_sat;

    rrc_bist #(.LATENCY(LATENCY), .LEN_LOG2(LEN_LOG2)) u_bist (
        .clk(clk), .rst_n(rst_n), .start(start), .abort(abort),
        .active(active), .core_clr(core_clr), .sample_en(sample_en),
        .din(din), .dout(dout),
        .done(done), .crc(crc), .n_out(n_out), .cycles(cycles), .n_sat(n_sat)
    );

    // same registered clear as dpl_top.v
    reg filt_rst_n;
    always @(posedge clk) filt_rst_n <= rst_n & ~core_clr;

    rrc_fir_core #(
        .N_TAPS(N_TAPS), .DATA_WIDTH(16), .COEFF_WIDTH(16),
        .COEFF_FILE("hw_demo/data/rrc_coeffs.hex")
    ) u_core (
        .clk(clk), .rst_n(filt_rst_n), .sample_en(sample_en), .pipe_en(1'b1),
        .din(din), .dout(dout)
    );

    integer errors = 0;
    integer gaps   = 0;

    // back to back: from the second RUN cycle on, sample_en must never drop
    always @(posedge clk)
        if (u_bist.state == 2'd2 && u_bist.n_in != 0 && !sample_en) gaps = gaps + 1;

    // a task's inout only copies back on return, so one task per signal
    task pulse_start;
        begin
            @(negedge clk) start = 1'b1;
            @(negedge clk) start = 1'b0;
        end
    endtask

    task pulse_abort;
        begin
            @(negedge clk) abort = 1'b1;
            @(negedge clk) abort = 1'b0;
        end
    endtask

    task run_and_check(input [8*16-1:0] tag);
        integer guard;
        begin
            pulse_start;
            guard = 0;
            while (!done && guard < (1 << LEN_LOG2) + 1000) begin
                @(posedge clk);
                guard = guard + 1;
            end
            @(negedge clk);
            if (!done) begin
                $display("FAIL (%0s): no done", tag);
                errors = errors + 1;
            end
            $display("[%0s] crc %08h n_out %0d cycles %0d n_sat %0d", tag, crc, n_out, cycles, n_sat);
            if (crc !== `BIST_EXP_CRC)       begin errors = errors + 1; $display("  crc expected %08h", `BIST_EXP_CRC); end
            if (n_out !== `BIST_EXP_N_OUT)   begin errors = errors + 1; $display("  n_out expected %0d", `BIST_EXP_N_OUT); end
            if (cycles !== `BIST_EXP_CYCLES) begin errors = errors + 1; $display("  cycles expected %0d", `BIST_EXP_CYCLES); end
            if (n_sat !== `BIST_EXP_N_SAT)   begin errors = errors + 1; $display("  n_sat expected %0d", `BIST_EXP_N_SAT); end
            if (active) begin errors = errors + 1; $display("  still active after done"); end
        end
    endtask

    initial begin
        repeat (3) @(posedge clk);
        rst_n = 1'b1;
        repeat (5) @(posedge clk);

        run_and_check("first run");

        // abort half way, then a clean run must still match exactly
        pulse_start;
        repeat ((1 << LEN_LOG2) / 2) @(posedge clk);
        pulse_abort;
        repeat (20) @(posedge clk);
        if (active || done) begin
            errors = errors + 1;
            $display("FAIL: abort did not stop the run");
        end
        run_and_check("after abort");
        run_and_check("third run");

        if (gaps) begin
            errors = errors + 1;
            $display("FAIL: %0d gap cycles in sample_en", gaps);
        end
        $display("");
        $display("rrc_bist: %0d samples per run back to back, %0d errors", 1 << LEN_LOG2, errors);
        if (errors == 0) $display("PASS");
        else             $display("FAIL");
        if (errors) $fatal(1, "tb_rrc_bist failed");
        $finish;
    end

endmodule
