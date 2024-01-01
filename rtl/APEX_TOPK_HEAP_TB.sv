//=============================================================================
// APEX_TOPK_HEAP_TB — Oracle-based correctness check for the dual-zone top-K
//
// Feeds a stream of random (score, chunk_id) descriptors into the heap, then
// reads back the retained entries and compares them against a sort-based
// reference: the K highest-scoring distinct chunk IDs seen in the stream.
//
// Tool: Icarus Verilog 12.0 (iverilog -g2012)
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_TOPK_HEAP_TB;

    //-------------------------------------------------------------------------
    // DUT signals
    //-------------------------------------------------------------------------
    logic                clk;
    logic                rst_n;

    logic [SCORE_W-1:0]  new_score;
    logic [ID_W-1:0]     new_chunk_id;
    logic                new_valid;

    logic                admitted;
    logic                admitted_valid;
    logic                case2_stall;
    logic                hold;

    logic                readout_start;
    logic [ID_W-1:0]     readout_chunk_id;
    logic [SCORE_W-1:0]  readout_score;
    logic                readout_valid;
    logic                readout_done;

    logic                flush;
    logic [4:0]          heap_count;
    logic                heap_idle;

    //-------------------------------------------------------------------------
    // Clock: 1 GHz
    //-------------------------------------------------------------------------
    initial begin
        clk = 1'b0;
        forever #0.5 clk = ~clk;
    end

    //-------------------------------------------------------------------------
    // DUT instantiation
    //-------------------------------------------------------------------------
    APEX_TOPK_HEAP #(
        .K            (K_ENTRIES),
        .EZ_SIZE      (EZ_SIZE),
        .SZ_SIZE      (SZ_SIZE),
        .SCORE_W      (SCORE_W),
        .ID_W         (ID_W),
        .BURST_THRESH (12)
    ) dut (
        .clk              (clk),
        .rst_n            (rst_n),
        .new_score        (new_score),
        .new_chunk_id     (new_chunk_id),
        .new_valid        (new_valid),
        .hold             (hold),
        .admitted         (admitted),
        .admitted_valid   (admitted_valid),
        .case2_stall      (case2_stall),
        .readout_start    (readout_start),
        .readout_chunk_id (readout_chunk_id),
        .readout_score    (readout_score),
        .readout_valid    (readout_valid),
        .readout_done     (readout_done),
        .flush            (flush),
        .heap_count       (heap_count),
        .heap_idle        (heap_idle)
    );

    //-------------------------------------------------------------------------
    // Test data storage
    //-------------------------------------------------------------------------
    localparam int MAX_INPUTS = 300;
    localparam int NUM_TESTS  = 5;

    logic [SCORE_W-1:0] in_score  [0:MAX_INPUTS-1];
    logic [ID_W-1:0]    in_id     [0:MAX_INPUTS-1];

    logic [SCORE_W-1:0] ref_score [0:K_ENTRIES-1];
    logic [ID_W-1:0]    ref_id    [0:K_ENTRIES-1];

    logic [SCORE_W-1:0] got_score [0:K_ENTRIES-1];
    logic [ID_W-1:0]    got_id    [0:K_ENTRIES-1];

    //-------------------------------------------------------------------------
    // Deterministic randomness ($random is portable across simulators)
    //-------------------------------------------------------------------------
    integer rng_state = 32'h1234_5678;

    function automatic logic [SCORE_W-1:0] rand_score();
        integer raw;
        raw = $random(rng_state);
        rand_score = raw[SCORE_W-1:0];
    endfunction

    //-------------------------------------------------------------------------
    // Wait for a positive clock edge
    //-------------------------------------------------------------------------
    task automatic tick();
        @(posedge clk);
        #0.1;
    endtask

    task automatic reset_dut();
        rst_n <= 1'b0;
        flush <= 1'b0;
        hold  <= 1'b0;
        new_valid <= 1'b0;
        readout_start <= 1'b0;
        repeat (4) tick();
        rst_n <= 1'b1;
        tick();
    endtask

    task automatic check_invariants(input int in_idx);
        logic [SCORE_W-1:0] true_sm;
        logic [4:0]         true_smi;
        logic               ok;
        // Invariants only hold once the heap has been built and is in ADMIT.
        if (dut.count >= K_ENTRIES && dut.heap_state == 3'd5) begin
        ok = 1'b1;
        // Check EZ min-heap property.
        for (int n = 0; n < EZ_SIZE/2; n = n + 1) begin
            int l, r;
            l = 2*n+1; r = 2*n+2;
            if (l < EZ_SIZE && dut.ez_score[l] < dut.ez_score[n]) ok = 1'b0;
            if (r < EZ_SIZE && dut.ez_score[r] < dut.ez_score[n]) ok = 1'b0;
        end
        // Check safe_min equals true min of SZ.
        true_sm = dut.sz_score[0];
        true_smi = 5'd0;
        for (int n = 1; n < SZ_SIZE; n = n + 1) begin
            if (dut.sz_score[n] < true_sm) begin
                true_sm = dut.sz_score[n];
                true_smi = 5'(n);
            end
        end
        if (dut.safe_min !== true_sm || dut.safe_min_idx !== true_smi) ok = 1'b0;
        if (!ok) begin
            $display("  INVARIANT VIOLATION after input %0d", in_idx);
            $display("    safe_min=%0d idx=%0d true=%0d idx=%0d",
                     dut.safe_min, dut.safe_min_idx, true_sm, true_smi);
            $display("    EZ:");
            for (int n = 0; n < EZ_SIZE; n = n + 1)
                $display("      %0d: id=%0d score=%0d", n, dut.ez_id[n], dut.ez_score[n]);
            $display("    SZ:");
            for (int n = 0; n < SZ_SIZE; n = n + 1)
                $display("      %0d: id=%0d score=%0d", n, dut.sz_id[n], dut.sz_score[n]);
        end
        end
    endtask

    task automatic feed_descriptor(input logic [SCORE_W-1:0] s,
                                   input logic [ID_W-1:0]    id,
                                   input int                 in_idx);
        // Wait until the heap can accept a new descriptor.
        while (!dut.admission_ready) tick();
        new_valid    <= 1'b1;
        new_score    <= s;
        new_chunk_id <= id;
        tick();
        new_valid    <= 1'b0;
        // Give the heap time to classify + update state.
        repeat (4) tick();
        if (in_idx >= 0) check_invariants(in_idx);
    endtask

    task automatic readback(input int expected_count,
                            output int actual_count);
        actual_count = 0;
        readout_start <= 1'b1;
        tick();
        readout_start <= 1'b0;

        // Collect up to expected_count valid beats.
        begin
            int c;
            c = 0;
            while (c < expected_count + 4) begin
                tick();
                if (readout_valid) begin
                    if (actual_count < K_ENTRIES) begin
                        got_score[actual_count] <= readout_score;
                        got_id[actual_count]    <= readout_chunk_id;
                    end
                    actual_count = actual_count + 1;
                end
                if (readout_done) c = expected_count + 4;
                else              c = c + 1;
            end
        end
    endtask

    //-------------------------------------------------------------------------
    // In-place bubble sort of reference arrays (descending by score, id tie)
    //-------------------------------------------------------------------------
    task automatic sort_ref(input int n);
        int i, j;
        logic [SCORE_W-1:0] ts;
        logic [ID_W-1:0]    tid;
        for (i = 0; i < n - 1; i = i + 1) begin
            for (j = 0; j < n - 1 - i; j = j + 1) begin
                if ({in_score[j], in_id[j]} < {in_score[j+1], in_id[j+1]}) begin
                    ts          = in_score[j];
                    in_score[j] = in_score[j+1];
                    in_score[j+1] = ts;

                    tid       = in_id[j];
                    in_id[j]  = in_id[j+1];
                    in_id[j+1] = tid;
                end
            end
        end
    endtask

    //-------------------------------------------------------------------------
    // Directed test: strictly monotonically increasing scores.
    // Every descriptor after the initial K=25 fill triggers Case 2 (x >
    // safe_min), i.e. a back-to-back cross-zone-replacement burst — the exact
    // scenario the restored single-cycle safe_min stall must handle without
    // recall loss. Retained set must equal the top-25 (the last 25 fed).
    //-------------------------------------------------------------------------
    task automatic test_monotonic_case2(output int pass);
        int n_inputs;
        int expected_count;
        int actual_count;
        n_inputs = 60;
        pass = 1;

        reset_dut();
        for (int i = 0; i < n_inputs; i = i + 1) begin
            in_score[i] = SCORE_W'(16'd100 + i * 16'd10);  // strictly increasing
            in_id[i]    = ID_W'(i + 7 * MAX_INPUTS);       // unique, disjoint ids
        end
        for (int i = 0; i < n_inputs; i = i + 1)
            feed_descriptor(in_score[i], in_id[i], -1);
        repeat (20) tick();

        // Oracle: the 25 highest are simply the last 25 (monotone increasing).
        expected_count = K_ENTRIES;
        for (int i = 0; i < expected_count; i = i + 1) begin
            ref_score[i] = in_score[n_inputs - 1 - i];
            ref_id[i]    = in_id[n_inputs - 1 - i];
        end

        readback(expected_count, actual_count);

        if (actual_count !== expected_count) begin
            $display("  FAIL monotonic: count mismatch (expected %0d, got %0d)",
                     expected_count, actual_count);
            pass = 0;
        end else begin
            for (int r = 0; r < expected_count; r = r + 1) begin
                int found; found = 0;
                for (int g = 0; g < actual_count; g = g + 1)
                    if ((got_id[g] === ref_id[r]) && (got_score[g] === ref_score[r]))
                        found = 1;
                if (!found) begin
                    $display("  FAIL monotonic: missing top-K entry id=%0d score=%0d",
                             ref_id[r], ref_score[r]);
                    pass = 0;
                end
            end
        end
        if (pass)
            $display("  PASS monotonic Case 2 burst: 60 increasing -> exact top-25, zero recall loss");
    endtask

    //-------------------------------------------------------------------------
    // Main test sequence
    //-------------------------------------------------------------------------
    int total_passed = 0;
    int total_failed = 0;

    initial begin
        $display("=== APEX_TOPK_HEAP Oracle Testbench ===");
        rng_state = 32'hACE1_2024;

        for (int t = 0; t < NUM_TESTS; t = t + 1) begin
            int n_inputs;
            int expected_count;
            int actual_count;
            int pass;

            // Vary test sizes to exercise fill (<K) and admit (>=K) paths.
            case (t)
                0: n_inputs = K_ENTRIES - 5;
                1: n_inputs = K_ENTRIES;
                2: n_inputs = K_ENTRIES + 50;
                3: n_inputs = 200;
                4: n_inputs = MAX_INPUTS;
            endcase

            reset_dut();

            // Generate inputs with unique IDs.
            for (int i = 0; i < n_inputs; i = i + 1) begin
                in_score[i] = rand_score();
                in_id[i]    = ID_W'(i + t * MAX_INPUTS);
            end

            if (t == 2) begin
                $display("=== Test 2 inputs ===");
                for (int i = 0; i < n_inputs; i = i + 1)
                    $display("  %0d: id=%0d score=%0d", i, in_id[i], in_score[i]);
            end

            // Feed descriptors.
            for (int i = 0; i < n_inputs; i = i + 1) begin
                feed_descriptor(in_score[i], in_id[i], (t == 2) ? i : -1);
            end

            // Wait for pipeline to drain.
            repeat (20) tick();

            // Compute reference top-K.
            sort_ref(n_inputs);
            expected_count = (n_inputs < K_ENTRIES) ? n_inputs : K_ENTRIES;

            for (int i = 0; i < expected_count; i = i + 1) begin
                ref_score[i] = in_score[i];
                ref_id[i]    = in_id[i];
            end

            // Read back retained entries.
            readback(expected_count, actual_count);

            // Compare as sets (order does not matter).
            pass = 1;
            if (actual_count !== expected_count) begin
                $display("  FAIL test %0d: count mismatch (expected %0d, got %0d)",
                         t, expected_count, actual_count);
                pass = 0;
            end else begin
                for (int r = 0; r < expected_count; r = r + 1) begin
                    int found;
                    found = 0;
                    for (int g = 0; g < actual_count; g = g + 1) begin
                        if ((got_id[g] === ref_id[r]) &&
                            (got_score[g] === ref_score[r])) begin
                            found = 1;
                        end
                    end
                    if (!found) begin
                        $display("  FAIL test %0d: missing ref entry id=%0d score=%0d",
                                 t, ref_id[r], ref_score[r]);
                        pass = 0;
                    end
                end
            end

            if (pass) begin
                $display("  PASS test %0d: %0d inputs -> %0d retained entries match sort oracle",
                         t, n_inputs, expected_count);
                total_passed = total_passed + 1;
            end else begin
                $display("  FAIL test %0d: retained set does not match sort oracle", t);
                $display("    Retained (id/score):");
                for (int g = 0; g < actual_count; g = g + 1)
                    $display("      %0d: id=%0d score=%0d", g, got_id[g], got_score[g]);
                $display("    Reference top-%0d (id/score):", expected_count);
                for (int r = 0; r < expected_count; r = r + 1)
                    $display("      %0d: id=%0d score=%0d", r, ref_id[r], ref_score[r]);
                total_failed = total_failed + 1;
            end

            // Small idle gap before next test.
            repeat (5) tick();
        end

        // Directed monotonic Case 2 burst (restored-stall correctness).
        begin
            int mono_pass;
            $display("=== Directed test: monotonic Case 2 burst ===");
            test_monotonic_case2(mono_pass);
            if (mono_pass) total_passed = total_passed + 1;
            else           total_failed = total_failed + 1;
        end

        $display("=== Results: %0d PASS, %0d FAIL ===", total_passed, total_failed);
        if (total_failed == 0)
            $display("ALL TOP-K ORACLE TESTS PASSED");
        else
            $display("TOP-K ORACLE TESTS FAILED");

        $finish;
    end

endmodule
