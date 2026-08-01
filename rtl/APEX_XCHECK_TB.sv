//=============================================================================
// APEX_XCHECK_TB — Trace-driven cross-check testbench
//
// Reads a descriptor trace (same format as experiments/_xcheck_out/
// xcheck_trace.txt) and drives each descriptor through APEX_PIPELINE, measuring
// the REAL end-to-end latency (S1-accept -> cpl_valid) and capturing the
// completion status. Emits one line per descriptor:
//
//     seq chunk_id status latency
//
// where status: 1=ADMITTED, 2=REJECTED; latency is the measured cycle count.
//
// This replaces the previously static, hand-authored xcheck_rtl_out.txt with
// numbers produced by an actual RTL simulation, so the Python cross-check's
// "rtl == model + 1" assertion is verifying real hardware behavior.
//
// Trace line format:
//   rejected_flag chunk_id epoch_match namespace_match s0 s1 s2 s3 s4 s5 s6
// epoch_match/namespace_match==0 forces a PCM reject (wrong epoch/namespace).
// s0..s6 are the 7 expert predictions for this descriptor; they are loaded into
// the internal APEX_EXPERT_BANK register files before the descriptor is run so
// the scoring path sees deterministic, trace-defined data.
//
// Plusargs:
//   +TRACE=<path>      input trace  (default: xcheck_trace.txt)
//   +OUT=<path>        output file  (default: xcheck_rtl_out.txt)
//   +XCHECK_DBG        enable per-descriptor heap-state diagnostics
//
// Tool: Icarus Verilog 12.0 (iverilog -g2012)
//=============================================================================
`timescale 1ns/1ps

module APEX_XCHECK_TB;

    logic clk = 0;
    logic rst_n = 0;
    always #0.5 clk = ~clk;

    // DUT signals
    logic        clk_en;
    logic [8:0]  cmd_chunk_id;
    logic [15:0] cmd_epoch;
    logic [7:0]  cmd_namespace;
    logic [7:0]  cmd_priority;
    logic        cmd_valid;
    logic        cmd_ready;

    logic [8:0]  cpl_chunk_id;
    logic [1:0]  cpl_status;
    logic        cpl_valid;
    logic        cpl_ready;

    logic [8:0]  dma_chunk_id;
    logic [15:0] dma_score;
    logic        dma_valid;
    logic        dma_ready;

    logic [8:0]  fb_chunk_id;
    logic [15:0] fb_attention_mass;
    logic [2:0]  fb_expert_id;
    logic        fb_valid;

    logic [15:0] cfg_current_epoch;
    logic [7:0]  cfg_current_namespace;
    logic [2:0]  cfg_eta_q;
    logic        cfg_flush;
    logic [6:0]  cfg_expert_active_mask;
    logic        cfg_sea_enable;

    logic [8:0]  res_set_id;
    logic        res_set_valid;
    logic [8:0]  res_clear_id;
    logic        res_clear_valid;

    logic        pipeline_idle;
    logic [31:0] stat_admitted;
    logic [31:0] stat_rejected;
    logic [31:0] stat_total_cycles;

    // OAT pin-table ports (paper §III-B, Invariant 1)
    logic [8:0]  reclaim_chunk_id = '0;
    logic [15:0] reclaim_generation = '0;
    logic        reclaim_allowed;
    logic        stat_pin_reject;
    logic [9:0]  stat_pin_count;

    APEX_PIPELINE dut (.*);

    integer cycle_count = 0;
    always @(posedge clk) cycle_count <= cycle_count + 1;

    // Config constants for this run
    localparam [15:0] GOOD_EPOCH = 16'h0001;
    localparam [15:0] BAD_EPOCH  = 16'hDEAD;
    localparam [7:0]  GOOD_NS    = 8'h01;
    localparam [7:0]  BAD_NS     = 8'hEE;

    // Trace storage
    localparam int MAX_TR = 4096;
    integer tr_rej   [0:MAX_TR-1];
    integer tr_chunk [0:MAX_TR-1];
    integer tr_em    [0:MAX_TR-1];
    integer tr_nm    [0:MAX_TR-1];
    integer tr_score [0:MAX_TR-1][0:6];
    integer n_tr;

    integer fout;
    string  trace_path;
    string  out_path;
    bit     dbg_enable;

    task automatic reset();
        rst_n = 0;
        clk_en = 1;
        cmd_valid = 0;
        cpl_ready = 1;
        dma_ready = 1;
        fb_valid = 0;
        fb_chunk_id = 0;
        fb_expert_id = 0;
        fb_attention_mass = 0;
        cfg_current_epoch = GOOD_EPOCH;
        cfg_current_namespace = GOOD_NS;
        cfg_eta_q = 3'd2;
        cfg_flush = 0;
        cfg_expert_active_mask = 7'b1111111;
        cfg_sea_enable = 1'b0;  // disable SEA probes for deterministic cross-check
        res_set_valid = 0;
        res_clear_valid = 0;
        cmd_chunk_id = 0;
        cmd_epoch = GOOD_EPOCH;
        cmd_namespace = GOOD_NS;
        cmd_priority = 0;
        repeat(5) @(posedge clk);
        rst_n = 1;
        repeat(2) @(posedge clk);
        // Pre-place every pool entry at GOOD_EPOCH so the directory CAS
        // admits descriptors that pass PCM validation. The pokes stand in
        // for the placement process of the evaluated pool.
        for (int c = 0; c < 512; c++) begin
            dut.u_directory.e_gen[c]      = GOOD_EPOCH;
            dut.u_directory.e_resident[c] = 1'b1;
        end
    endtask

    // Write the 7 expert predictions for a chunk into the internal expert banks
    // using the feedback write port.  The write is gated by pipeline_idle, so it
    // can only happen when the scoring path is drained.  Each bank is written
    // on a separate cycle because the feedback interface targets one expert at
    // a time.  The read-first bank semantics guarantee the values are visible
    // to the descriptor's S3 read on the following cycle.
    task automatic write_expert_predictions(input integer chunk,
                                            input integer s0, input integer s1,
                                            input integer s2, input integer s3,
                                            input integer s4, input integer s5,
                                            input integer s6);
        integer scores [0:6];
        integer e;
        scores[0] = s0; scores[1] = s1; scores[2] = s2;
        scores[3] = s3; scores[4] = s4; scores[5] = s5; scores[6] = s6;
        for (e = 0; e < 7; e = e + 1) begin
            @(negedge clk);
            fb_chunk_id       = chunk[8:0];
            fb_expert_id      = e[2:0];
            fb_attention_mass = scores[e][15:0];
            fb_valid          = 1'b1;
            @(posedge clk);
        end
        @(negedge clk);
        fb_valid          = 1'b0;
        fb_attention_mass = 16'd0;
        fb_expert_id      = 3'd0;
    endtask

    // Drive one descriptor, honoring cmd_ready backpressure; measure latency
    // from the accepting edge to the next cpl_valid.
    task automatic run_descriptor(input integer chunk, input integer em,
                                  input integer nm, output integer status,
                                  output integer latency);
        integer start_cycle;
        @(negedge clk);
        cmd_chunk_id  = chunk[8:0];
        cmd_epoch     = (em == 1) ? GOOD_EPOCH : BAD_EPOCH;
        cmd_namespace = (nm == 1) ? GOOD_NS   : BAD_NS;
        cmd_priority  = 8'd0;
        cmd_valid     = 1;
        do @(posedge clk); while (!cmd_ready);
        start_cycle = cycle_count;
        @(negedge clk);
        cmd_valid = 0;
        // Wait for completion
        status  = 0;
        latency = 0;
        while (!cpl_valid && (cycle_count - start_cycle) < 40)
            @(posedge clk);
        if (cpl_valid) begin
            latency = cycle_count - start_cycle;
            status  = (cpl_status == 2'b01) ? 1 : 2;
        end
    endtask

    integer i;
    integer st, lat;
    integer line_ok;
    integer f;
    integer code;
    integer rej, ch, em, nm, s0,s1,s2,s3,s4,s5,s6;

    initial begin
        if (!$value$plusargs("TRACE=%s", trace_path))
            trace_path = "xcheck_trace.txt";
        if (!$value$plusargs("OUT=%s", out_path))
            out_path = "xcheck_rtl_out.txt";
        dbg_enable = $test$plusargs("XCHECK_DBG");

        // Load trace
        n_tr = 0;
        f = $fopen(trace_path, "r");
        if (f == 0) begin
            $display("XCHECK_TB: cannot open trace %s", trace_path);
            $finish;
        end
        while (!$feof(f) && n_tr < MAX_TR) begin
            code = $fscanf(f, "%d %d %d %d %d %d %d %d %d %d %d\n",
                           rej, ch, em, nm, s0,s1,s2,s3,s4,s5,s6);
            if (code == 11) begin
                tr_rej[n_tr]   = rej;
                tr_chunk[n_tr] = ch;
                tr_em[n_tr]    = em;
                tr_nm[n_tr]    = nm;
                tr_score[n_tr][0] = s0;
                tr_score[n_tr][1] = s1;
                tr_score[n_tr][2] = s2;
                tr_score[n_tr][3] = s3;
                tr_score[n_tr][4] = s4;
                tr_score[n_tr][5] = s5;
                tr_score[n_tr][6] = s6;
                n_tr = n_tr + 1;
            end
        end
        $fclose(f);
        $display("XCHECK_TB: loaded %0d descriptors from %s", n_tr, trace_path);

        reset();

        fout = $fopen(out_path, "w");
        if (fout == 0) begin
            $display("XCHECK_TB: cannot open output %s", out_path);
            $finish;
        end
        $fwrite(fout, "# seq chunk_id status latency (trace-driven RTL run)\n");

        for (i = 0; i < n_tr; i = i + 1) begin
            // Drain to idle so each descriptor's latency is measured in
            // isolation (no completion aliasing from a pipelined predecessor).
            while (!pipeline_idle) @(posedge clk);
            // Load the 7 expert predictions for this descriptor into the internal
            // banks.  This mirrors the real system state where the banks already
            // hold the previous decode step's predictions before the descriptor
            // is submitted.
            write_expert_predictions(tr_chunk[i],
                                     tr_score[i][0], tr_score[i][1], tr_score[i][2],
                                     tr_score[i][3], tr_score[i][4], tr_score[i][5],
                                     tr_score[i][6]);
            // rejected_flag==0 means "submitted to pipeline" per trace semantics
            if (tr_rej[i] == 0) begin
                run_descriptor(tr_chunk[i], tr_em[i], tr_nm[i], st, lat);
                $fwrite(fout, "%0d %0d %0d %0d\n", i, tr_chunk[i], st, lat);
            end else begin
                st = 2;  // rejected_flag==1: descriptor was already filtered
                lat = 0;
            end
            // Optional debug: print heap state after each descriptor.
            if (dbg_enable)
                $display("XCHECK_DBG: desc=%0d chunk=%0d status=%0d lat=%0d heap_count=%0d ez_min=%0d safe_min=%0d",
                         i, tr_chunk[i], st, lat, dut.heap_count, dut.u_heap.ez_min, dut.u_heap.safe_min);
            repeat(2) @(posedge clk);
        end

        $fclose(fout);
        $display("XCHECK_TB: wrote RTL results to %s", out_path);
        $finish;
    end

    initial begin
        #200000;
        $display("XCHECK_TB TIMEOUT");
        $finish;
    end

endmodule
