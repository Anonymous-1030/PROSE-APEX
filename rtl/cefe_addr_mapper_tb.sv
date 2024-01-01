//=============================================================================
// Testbench for cefe_addr_mapper — long-context two-tier mapping (§III-C.c).
//
// Scenarios:
//   T1: install (id=100 -> ptr=0xABC), commit at step boundary, then a lookup
//       hits in Tier-1 in a single cycle with the right pointer.
//   T2: causal write discipline — an install WITHOUT a step_commit is NOT
//       visible to a lookup in the same step (reads see prior committed state).
//   T3: Tier-2 probe — after eviction of a Tier-1 slot (alias overwrite), the
//       original id still resolves via the 3-cycle backing probe.
//   T4: tag-validated zero fallback — a hash-aliasing id that was never
//       installed returns MISS (not a wrong pointer).
//=============================================================================
`timescale 1ns/1ps

module cefe_addr_mapper_tb;

    localparam int LOGICAL_ID_W = 12;
    localparam int PTR_W = 20;

    logic clk = 0, rst_n = 0;
    always #0.5 clk = ~clk;

    logic                    lookup_valid;
    logic [LOGICAL_ID_W-1:0] lookup_id;
    logic                    lookup_hit;
    logic [PTR_W-1:0]        lookup_ptr;
    logic                    lookup_miss;
    logic                    lookup_ready;
    logic [PTR_W-1:0]        probe_ptr;
    logic                    probe_found;
    logic                    install_valid;
    logic [LOGICAL_ID_W-1:0] install_id;
    logic [PTR_W-1:0]        install_ptr;
    logic                    step_commit;
    logic [31:0]             stat_hits, stat_misses;

    int errors = 0;

    // Latch the (single-cycle) Tier-2 probe result whenever it pulses, so the
    // test can check it without sampling on the exact cycle.
    logic             probe_seen;
    logic [PTR_W-1:0] probe_seen_ptr;
    logic             probe_seen_found;
    always @(posedge clk) begin
        if (!rst_n) begin
            probe_seen <= 1'b0;
        end else if (lookup_ready) begin
            probe_seen       <= 1'b1;
            probe_seen_ptr   <= probe_ptr;
            probe_seen_found <= probe_found;
        end
    end

    cefe_addr_mapper #(.LOGICAL_ID_W(LOGICAL_ID_W), .PTR_W(PTR_W)) dut (
        .clk(clk), .rst_n(rst_n),
        .lookup_valid(lookup_valid), .lookup_id(lookup_id),
        .lookup_hit(lookup_hit), .lookup_ptr(lookup_ptr),
        .lookup_miss(lookup_miss), .lookup_ready(lookup_ready),
        .probe_ptr(probe_ptr), .probe_found(probe_found),
        .install_valid(install_valid), .install_id(install_id),
        .install_ptr(install_ptr), .step_commit(step_commit),
        .stat_hits(stat_hits), .stat_misses(stat_misses)
    );

    task automatic install(input [LOGICAL_ID_W-1:0] id, input [PTR_W-1:0] ptr,
                           input logic commit);
        @(negedge clk);
        install_valid = 1; install_id = id; install_ptr = ptr;
        @(negedge clk);
        install_valid = 0;
        if (commit) begin
            step_commit = 1;
            @(negedge clk);
            step_commit = 0;
        end
    endtask

    task automatic do_lookup(input [LOGICAL_ID_W-1:0] id);
        @(negedge clk);
        lookup_valid = 1; lookup_id = id;
        @(negedge clk);
        lookup_valid = 0;
    endtask

    task automatic chk(input logic c, input string m);
        if (!c) begin $display("  [FAIL] %s", m); errors++; end
        else       $display("  [ok]   %s", m);
    endtask

    initial begin
        lookup_valid = 0; install_valid = 0; step_commit = 0;
        lookup_id = 0; install_id = 0; install_ptr = 0;
        repeat (4) @(negedge clk);
        rst_n = 1;
        @(negedge clk);

        $display("=== cefe_addr_mapper_tb ===");

        // T1: install + commit, then single-cycle hit.
        install(12'd100, 20'hABC, 1'b1);
        do_lookup(12'd100);
        #0.1;
        chk(lookup_hit && lookup_ptr == 20'hABC,
            "T1: committed mapping hits Tier-1 in 1 cycle with correct ptr");

        // T2: causal discipline — install WITHOUT commit is invisible this step.
        @(negedge clk);
        install_valid = 1; install_id = 12'd250; install_ptr = 20'h222;
        @(negedge clk);
        install_valid = 0;
        // lookup 250 before any step_commit -> must MISS
        lookup_valid = 1; lookup_id = 12'd250;
        @(negedge clk);
        lookup_valid = 0;
        #0.1;
        chk(lookup_hit == 1'b0,
            "T2: uncommitted install is NOT visible to same-step lookup (causal)");
        // now commit and it appears
        step_commit = 1; @(negedge clk); step_commit = 0;
        do_lookup(12'd250);
        #0.1;
        chk(lookup_hit && lookup_ptr == 20'h222,
            "T2: after step_commit the mapping becomes visible");

        // T3: Tier-2 probe survives a Tier-1 alias overwrite.
        // id=100 and id=710 hash to the SAME Tier-1 slot (411). Install 100,
        // then 710 (which evicts 100 from Tier-1 but not from the Tier-2
        // backing store). A lookup of 100 must then MISS Tier-1 and resolve via
        // the 3-cycle backing probe.
        install(12'd100, 20'hABC, 1'b1);
        install(12'd710, 20'hDEF, 1'b1);   // aliases slot 411, evicts 100 from T1
        probe_seen = 1'b0;
        do_lookup(12'd100);
        #0.1;
        chk(lookup_miss == 1'b1,
            "T3: aliased id=100 misses Tier-1 after id=710 overwrote its slot");
        // allow the 3-cycle backing probe to resolve; the monitor latches it
        repeat (4) @(negedge clk);
        #0.1;
        chk(probe_seen && probe_seen_found && probe_seen_ptr == 20'hABC,
            "T3: Tier-2 backing probe resolves id=100 (ptr=0xABC) in 3 cycles");

        // T4: tag-validated zero fallback — never-installed id returns MISS.
        do_lookup(12'd4000);
        #0.1;
        chk(lookup_hit == 1'b0,
            "T4: never-installed id returns MISS, not a wrong pointer");

        $display("=== cefe_addr_mapper_tb: %0d errors ===", errors);
        if (errors == 0) $display("PASS"); else $display("FAIL");
        $finish;
    end

endmodule
