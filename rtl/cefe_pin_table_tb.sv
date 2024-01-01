//=============================================================================
// Testbench for cefe_pin_table — verifies Invariant 1 (paper §III-B).
//
// Scenarios:
//   T1: allocate a pin for (chunk,gen); reclaim of that (chunk,gen) must be
//       FORBIDDEN while the pin is held.
//   T2: reclaim of a DIFFERENT (chunk,gen) is allowed (pin is generation-exact).
//   T3: after RELEASE, reclaim of the original (chunk,gen) becomes allowed.
//   T4: fill the table to NUM_ENTRIES; the next allocate reports alloc_ok=0
//       (the OAT must then reject — no pin installed, no payload).
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module cefe_pin_table_tb;

    localparam int NUM_ENTRIES = 8;   // small for exhaustive TB
    localparam int TENANT_W = 4, CHUNK_W = 9, GEN_W = 16;

    logic clk = 0, rst_n = 0;
    always #0.5 clk = ~clk;   // 1 GHz

    logic                 alloc_valid;
    logic [TENANT_W-1:0]  alloc_tenant;
    logic [CHUNK_W-1:0]   alloc_chunk;
    logic [GEN_W-1:0]     alloc_gen;
    logic                 alloc_ok;
    logic [$clog2(NUM_ENTRIES)-1:0] alloc_index;

    logic                 release_valid;
    logic [CHUNK_W-1:0]   release_chunk;
    logic [GEN_W-1:0]     release_gen;

    logic [CHUNK_W-1:0]   reclaim_chunk;
    logic [GEN_W-1:0]     reclaim_gen;
    logic                 reclaim_allowed;

    logic [$clog2(NUM_ENTRIES+1)-1:0] pin_count;
    logic                 table_full;

    int errors = 0;

    cefe_pin_table #(.NUM_ENTRIES(NUM_ENTRIES)) dut (
        .clk(clk), .rst_n(rst_n),
        .alloc_valid(alloc_valid), .alloc_tenant(alloc_tenant),
        .alloc_chunk(alloc_chunk), .alloc_gen(alloc_gen),
        .alloc_ok(alloc_ok), .alloc_index(alloc_index),
        .release_valid(release_valid), .release_chunk(release_chunk),
        .release_gen(release_gen),
        .reclaim_chunk(reclaim_chunk), .reclaim_gen(reclaim_gen),
        .reclaim_allowed(reclaim_allowed),
        .pin_count(pin_count), .table_full(table_full)
    );

    task automatic do_alloc(input [CHUNK_W-1:0] c, input [GEN_W-1:0] gg,
                            input [TENANT_W-1:0] t);
        @(negedge clk);
        alloc_valid = 1; alloc_chunk = c; alloc_gen = gg; alloc_tenant = t;
        @(negedge clk);
        alloc_valid = 0;
    endtask

    task automatic do_release(input [CHUNK_W-1:0] c, input [GEN_W-1:0] gg);
        @(negedge clk);
        release_valid = 1; release_chunk = c; release_gen = gg;
        @(negedge clk);
        release_valid = 0;
    endtask

    task automatic check(input logic cond, input string msg);
        if (!cond) begin
            $display("  [FAIL] %s", msg);
            errors = errors + 1;
        end else begin
            $display("  [ok]   %s", msg);
        end
    endtask

    initial begin
        alloc_valid = 0; release_valid = 0;
        alloc_chunk = 0; alloc_gen = 0; alloc_tenant = 0;
        release_chunk = 0; release_gen = 0;
        reclaim_chunk = 0; reclaim_gen = 0;
        repeat (4) @(negedge clk);
        rst_n = 1;
        @(negedge clk);

        $display("=== cefe_pin_table_tb ===");

        // T1: pin (chunk=42, gen=7); reclaim of it must be forbidden.
        do_alloc(9'd42, 16'd7, 4'd1);
        reclaim_chunk = 9'd42; reclaim_gen = 16'd7;
        #0.1;
        check(reclaim_allowed == 1'b0,
              "T1: reclaim of pinned (42,7) is FORBIDDEN (Invariant 1)");
        check(pin_count == 1, "T1: pin_count == 1");

        // T2: reclaim of a different generation of the same chunk is allowed.
        reclaim_chunk = 9'd42; reclaim_gen = 16'd8;
        #0.1;
        check(reclaim_allowed == 1'b1,
              "T2: reclaim of (42,8) allowed — pin is generation-exact");
        // ... and a different chunk entirely.
        reclaim_chunk = 9'd99; reclaim_gen = 16'd7;
        #0.1;
        check(reclaim_allowed == 1'b1, "T2: reclaim of (99,7) allowed");

        // T3: after RELEASE(42,7), reclaim of (42,7) becomes allowed.
        do_release(9'd42, 16'd7);
        reclaim_chunk = 9'd42; reclaim_gen = 16'd7;
        #0.1;
        check(reclaim_allowed == 1'b1,
              "T3: after RELEASE, reclaim of (42,7) allowed");
        check(pin_count == 0, "T3: pin_count back to 0");

        // T4: fill the table; next allocate must report alloc_ok=0.
        for (int k = 0; k < NUM_ENTRIES; k++) begin
            do_alloc(9'(100 + k), 16'd1, 4'd2);
        end
        check(table_full == 1'b1, "T4: table_full after NUM_ENTRIES allocs");
        @(negedge clk);
        alloc_valid = 1; alloc_chunk = 9'd200; alloc_gen = 16'd1; alloc_tenant = 4'd3;
        #0.1;
        check(alloc_ok == 1'b0,
              "T4: allocate on full table reports alloc_ok=0 (OAT must reject)");
        @(negedge clk);
        alloc_valid = 0;

        $display("=== cefe_pin_table_tb: %0d errors ===", errors);
        if (errors == 0) $display("PASS");
        else             $display("FAIL");
        $finish;
    end

endmodule
