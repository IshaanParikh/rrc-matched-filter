// data-in-the-loop testbench for the AXI4-Stream RRC filter
//
// checks: X at power-up cleared by reset, output bit-exact vs
// python/golden_model.py, no data loss under random back-pressure,
// AXI4-Stream legality on the master port, measured latency == LATENCY
//
// run from the repo root, paths in stimulus_meta.vh are repo-relative
`timescale 1ns / 1ps
`include "rrc_defs.vh"
`include "stimulus_meta.vh"

module tb_rrc_fir_axis;

    localparam N_TAPS      = `STIM_N_TAPS;
    localparam NUM_SAMPLES = `STIM_NUM_SAMPLES;
    localparam DATA_WIDTH  = 16;
    localparam AXIS_WIDTH  = 2*DATA_WIDTH;
    localparam TREE_LEVELS = `RRC_CLOG2(N_TAPS);
    localparam LATENCY     = 1 + `BOOTH_R4_LATENCY + TREE_LEVELS + 1;
    localparam FLUSH       = LATENCY + 16;      // extra beats to drain the pipe

    reg clk = 1'b0;
    reg rst_n;
    always #2.0 clk = ~clk;                     // 250 MHz

    reg                    s_axis_tvalid;
    wire                   s_axis_tready;
    reg  [AXIS_WIDTH-1:0]  s_axis_tdata;
    reg                    s_axis_tlast;

    wire                   m_axis_tvalid;
    reg                    m_axis_tready;
    wire [AXIS_WIDTH-1:0]  m_axis_tdata;
    wire                   m_axis_tlast;

    rrc_fir_axis #(
        .N_TAPS      (N_TAPS),
        .DATA_WIDTH  (DATA_WIDTH),
        .COEFF_WIDTH (16),
        .COEFF_FILE  (`STIM_COEFF_FILE)
    ) dut (
        .clk           (clk),
        .rst_n         (rst_n),
        .s_axis_tvalid (s_axis_tvalid),
        .s_axis_tready (s_axis_tready),
        .s_axis_tdata  (s_axis_tdata),
        .s_axis_tlast  (s_axis_tlast),
        .m_axis_tvalid (m_axis_tvalid),
        .m_axis_tready (m_axis_tready),
        .m_axis_tdata  (m_axis_tdata),
        .m_axis_tlast  (m_axis_tlast)
    );

    reg [AXIS_WIDTH-1:0] stim    [0:NUM_SAMPLES-1];
    reg [AXIS_WIDTH-1:0] golden  [0:NUM_SAMPLES-1];
    reg [AXIS_WIDTH-1:0] capture [0:NUM_SAMPLES-1];

    integer errors      = 0;
    integer n_captured  = 0;
    integer n_accepted  = 0;
    integer n_mismatch  = 0;
    integer n_stall_cyc = 0;
    integer n_gap_cyc   = 0;
    integer last_seen   = 0;

    reg bp_enable  = 1'b0;
    reg gap_enable = 1'b0;

    // +seed=N picks a different back-pressure / gap pattern. without it the
    // pattern is the global $random stream, same as it always was
    integer seed     = 0;
    reg     use_seed = 1'b0;
    initial if ($value$plusargs("seed=%d", seed)) begin
        use_seed = 1'b1;
        $display("[tb] stall seed %0d", seed);
    end

    function integer rnd;
        input dummy;
        begin
            if (use_seed) rnd = $random(seed);
            else          rnd = $random;
        end
    endfunction

    // random TREADY once bp_enable is set
    always @(negedge clk) begin
        if (!rst_n)          m_axis_tready <= 1'b1;
        else if (!bp_enable) m_axis_tready <= 1'b1;
        else                 m_axis_tready <= (({rnd(0)} % 100) < 65);
    end

    always @(posedge clk)
        if (rst_n && !m_axis_tready) n_stall_cyc = n_stall_cyc + 1;

    // capture every accepted master beat
    always @(posedge clk) begin
        if (rst_n && m_axis_tvalid && m_axis_tready) begin
            if (n_captured < NUM_SAMPLES) begin
                capture[n_captured] = m_axis_tdata;
                if (m_axis_tlast) last_seen = n_captured;
            end
            n_captured = n_captured + 1;
        end
    end

    // master-port protocol checker
    reg                   pv_valid = 1'b0;
    reg                   pv_ready = 1'b0;
    reg                   pv_last  = 1'b0;
    reg [AXIS_WIDTH-1:0]  pv_data  = {AXIS_WIDTH{1'b0}};

    always @(posedge clk) begin
        if (rst_n) begin
            if (pv_valid && !pv_ready) begin
                if (m_axis_tvalid !== 1'b1) begin
                    $display("  PROTOCOL ERROR t=%0t: TVALID dropped without a handshake", $time);
                    errors = errors + 1;
                end
                if (m_axis_tdata !== pv_data) begin
                    $display("  PROTOCOL ERROR t=%0t: TDATA changed while stalled", $time);
                    errors = errors + 1;
                end
                if (m_axis_tlast !== pv_last) begin
                    $display("  PROTOCOL ERROR t=%0t: TLAST changed while stalled", $time);
                    errors = errors + 1;
                end
            end
        end
        pv_valid <= rst_n ? m_axis_tvalid : 1'b0;
        pv_ready <= m_axis_tready;
        pv_data  <= m_axis_tdata;
        pv_last  <= m_axis_tlast;
    end

    // latency = cycles between first accepted beat and first TVALID
    integer cyc = 0, first_accept_cyc = -1, first_valid_cyc = -1;
    always @(posedge clk) begin
        if (rst_n) begin
            cyc = cyc + 1;
            if (s_axis_tvalid && s_axis_tready) begin
                n_accepted = n_accepted + 1;
                if (first_accept_cyc < 0) first_accept_cyc = cyc;
            end
            if (m_axis_tvalid && first_valid_cyc < 0) first_valid_cyc = cyc;
            if (!s_axis_tvalid) n_gap_cyc = n_gap_cyc + 1;
        end
    end

    task check_all_x;                       // every datapath reg must be X
        input [255:0] tag;
        begin
            if (^dut.u_fir_i.shift_reg[0]      !== 1'bx ||
                ^dut.u_fir_q.shift_reg[N_TAPS-1] !== 1'bx ||
                ^dut.u_fir_i.dout_r            !== 1'bx ||
                ^dut.u_fir_i.u_tree.node[TREE_LEVELS][0] !== 1'bx ||
                ^m_axis_tdata                  !== 1'bx) begin
                $display("  ERROR (%0s): expected X in the datapath before reset", tag);
                errors = errors + 1;
            end else begin
                $display("[tb] %0s: delay line / Booth pipe / tree / TDATA all X, as expected", tag);
            end
        end
    endtask

    task check_all_zero;                    // reset must clear them
        input [255:0] tag;
        begin
            if (dut.u_fir_i.shift_reg[0]      !== {DATA_WIDTH{1'b0}} ||
                dut.u_fir_q.shift_reg[N_TAPS-1] !== {DATA_WIDTH{1'b0}} ||
                dut.u_fir_i.dout_r            !== {DATA_WIDTH{1'b0}} ||
                m_axis_tdata                  !== {AXIS_WIDTH{1'b0}} ||
                m_axis_tvalid                 !== 1'b0) begin
                $display("  ERROR (%0s): synchronous reset did not clear the pipeline", tag);
                errors = errors + 1;
            end else begin
                $display("[tb] %0s: pipeline cleared to 0 by synchronous reset", tag);
            end
        end
    endtask

    integer si, gi, ngap, i;
    integer n_x_out;

    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("data/tb_rrc_fir_axis.vcd");
            $dumpvars(0, tb_rrc_fir_axis);
        end

        $readmemh(`STIM_IQ_FILE, stim);
        $readmemh(`STIM_GOLDEN_FILE, golden);
        if (^stim[0] === 1'bx || ^stim[NUM_SAMPLES-1] === 1'bx)
            $fatal(1, "stimulus file %s did not load %0d samples", `STIM_IQ_FILE, NUM_SAMPLES);
        if (^golden[0] === 1'bx || ^golden[NUM_SAMPLES-1] === 1'bx)
            $fatal(1, "%s did not load %0d samples, run python/golden_model.py",
                   `STIM_GOLDEN_FILE, NUM_SAMPLES);

        $display("");
        $display("[tb] RRC FIR AXI4-Stream, %0d taps, %0d samples, LATENCY=%0d",
                 N_TAPS, NUM_SAMPLES, LATENCY);

        s_axis_tvalid = 1'b0;
        s_axis_tdata  = {AXIS_WIDTH{1'b0}};
        s_axis_tlast  = 1'b0;

        // power-up, no reset yet, so X everywhere
        rst_n = 1'b1;
        repeat (4) @(posedge clk);
        @(negedge clk);
        check_all_x("power-up");

        // sync reset
        rst_n = 1'b0;
        @(posedge clk);                     // this edge clears the pipeline
        @(negedge clk);
        check_all_zero("after 1 reset edge");
        repeat (3) @(posedge clk);
        @(negedge clk);
        rst_n = 1'b1;

        // stream, clean at first so latency is measurable
        $display("[tb] streaming %0d samples (clean for the first 64 beats, then random TREADY/TVALID stress)",
                 NUM_SAMPLES);

        for (si = 0; si < NUM_SAMPLES + FLUSH; si = si + 1) begin

            if (si == 64) begin             // stress on once latency is captured
                bp_enable  = 1'b1;
                gap_enable = 1'b1;
            end

            // random idle gaps on the source
            if (gap_enable && (({rnd(0)} % 100) < 25)) begin
                ngap = 1 + ({rnd(0)} % 3);
                for (gi = 0; gi < ngap; gi = gi + 1) begin
                    @(negedge clk);
                    s_axis_tvalid = 1'b0;
                    s_axis_tdata  = {AXIS_WIDTH{1'bx}};   // must be ignored
                    @(posedge clk);
                end
            end

            @(negedge clk);
            s_axis_tvalid = 1'b1;
            s_axis_tdata  = (si < NUM_SAMPLES) ? stim[si] : {AXIS_WIDTH{1'b0}};
            s_axis_tlast  = (si == NUM_SAMPLES-1);
            @(posedge clk);
            while (!s_axis_tready) @(posedge clk);        // hold until accepted
        end

        @(negedge clk);
        s_axis_tvalid = 1'b0;
        s_axis_tlast  = 1'b0;
        s_axis_tdata  = {AXIS_WIDTH{1'b0}};

        // let anything still in flight drain
        bp_enable = 1'b0;
        repeat (4*LATENCY) @(posedge clk);

        // compare against the golden model
        n_x_out = 0;
        for (i = 0; i < NUM_SAMPLES; i = i + 1) begin
            if (^capture[i] === 1'bx) n_x_out = n_x_out + 1;
            else if (capture[i] !== golden[i]) begin
                n_mismatch = n_mismatch + 1;
                if (n_mismatch <= 10)
                    $display("  MISMATCH sample %0d: rtl=%08h golden=%08h", i, capture[i], golden[i]);
            end
        end

        $writememh(`STIM_OUT_FILE, capture);

        // 1-in-1-out filter, so beat counts must agree exactly. FLUSH beats
        // past NUM_SAMPLES are real transfers too, they push the tail out.
        if (n_captured != n_accepted) begin
            $display("  ERROR: %0d beats in but %0d out, data lost or duplicated",
                     n_accepted, n_captured);
            errors = errors + 1;
        end
        if (n_accepted != NUM_SAMPLES + FLUSH) begin
            $display("  ERROR: source placed %0d beats, expected %0d",
                     n_accepted, NUM_SAMPLES + FLUSH);
            errors = errors + 1;
        end
        if (n_x_out != 0) begin
            $display("  ERROR: %0d output beats contained X", n_x_out);
            errors = errors + 1;
        end
        if (n_mismatch != 0) errors = errors + n_mismatch;
        if (last_seen != NUM_SAMPLES-1) begin
            $display("  ERROR: TLAST landed on beat %0d, expected %0d", last_seen, NUM_SAMPLES-1);
            errors = errors + 1;
        end
        if ((first_valid_cyc - first_accept_cyc) != LATENCY) begin
            $display("  ERROR: measured latency %0d, expected %0d",
                     first_valid_cyc - first_accept_cyc, LATENCY);
            errors = errors + 1;
        end

        $display("");
        $display("============ tb_rrc_fir_axis summary ============");
        $display(" taps / latency      : %0d taps, %0d cycles (measured %0d)",
                 N_TAPS, LATENCY, first_valid_cyc - first_accept_cyc);
        $display(" beats in / out      : %0d / %0d (must be equal: no loss, no duplicates)",
                 n_accepted, n_captured);
        $display("   of which checked  : %0d against the golden model", NUM_SAMPLES);
        $display(" TREADY stall cycles : %0d", n_stall_cyc);
        $display(" TVALID gap cycles   : %0d", n_gap_cyc);
        $display(" golden mismatches   : %0d", n_mismatch);
        $display(" X in output stream  : %0d", n_x_out);
        $display(" TLAST beat          : %0d", last_seen);
        $display(" wrote               : %0s", `STIM_OUT_FILE);
        $display(" errors              : %0d", errors);
        $display(" RESULT              : %0s", (errors == 0) ? "PASS" : "FAIL");
        $display("================================================");
        $display("");

        if (errors != 0) $fatal(1, "tb_rrc_fir_axis failed");
        $finish;
    end

    initial begin
        #20_000_000;
        $display("ERROR: tb_rrc_fir_axis timeout (captured %0d/%0d)", n_captured, NUM_SAMPLES);
        $fatal(1, "timeout");
    end

endmodule
