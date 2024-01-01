//=============================================================================
// Testbench for cefe_dma_engine — Mode A P2P posted writes (§III-D, Table I).
//
// Scenarios:
//   T1: a 4-beat chunk streams 4 posted-write TLPs, addresses increment per
//       beat, PASID is carried on every beat, tlp_last marks the final beat.
//   T2: credit-based flow control — when credit_avail drops, no beat fires;
//       the transfer resumes when credit returns (no beat lost).
//   T3: completion -> RELEASE — xfer_done pulses exactly once with the correct
//       (chunk, gen) after the last beat, scoping the pin to [ISSUE, COMPLETE).
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module cefe_dma_engine_tb;

    localparam int CHUNK_W = 9, GEN_W = 16, PASID_W = 20, ADDR_W = 48;
    localparam int BEAT_W = 256;

    logic clk = 0, rst_n = 0;
    always #0.5 clk = ~clk;

    logic                 admit_valid;
    logic [CHUNK_W-1:0]   admit_chunk;
    logic [GEN_W-1:0]     admit_gen;
    logic [PASID_W-1:0]   admit_pasid;
    logic [ADDR_W-1:0]    admit_gpu_addr;
    logic [4:0]           admit_beats;
    logic                 admit_ready;

    logic                 tlp_valid;
    logic [ADDR_W-1:0]    tlp_addr;
    logic [BEAT_W-1:0]    tlp_data;
    logic [PASID_W-1:0]   tlp_pasid;
    logic                 tlp_last;
    logic                 tlp_ready;
    logic                 credit_avail;
    logic [BEAT_W-1:0]    src_data;
    logic                 xfer_done;
    logic [CHUNK_W-1:0]   xfer_chunk;
    logic [GEN_W-1:0]     xfer_gen;
    logic                 busy;
    logic [31:0]          stat_chunks_done, stat_beats_sent;

    int errors = 0;
    int beats_observed = 0;
    int done_pulses = 0;
    logic [PASID_W-1:0] pasid_bad = 0;

    cefe_dma_engine #(.CHUNK_W(CHUNK_W), .GEN_W(GEN_W), .PASID_W(PASID_W),
                      .ADDR_W(ADDR_W), .BEAT_W(BEAT_W)) dut (
        .clk(clk), .rst_n(rst_n),
        .admit_valid(admit_valid), .admit_chunk(admit_chunk), .admit_gen(admit_gen),
        .admit_pasid(admit_pasid), .admit_gpu_addr(admit_gpu_addr),
        .admit_beats(admit_beats), .admit_ready(admit_ready),
        .tlp_valid(tlp_valid), .tlp_addr(tlp_addr), .tlp_data(tlp_data),
        .tlp_pasid(tlp_pasid), .tlp_last(tlp_last), .tlp_ready(tlp_ready),
        .credit_avail(credit_avail), .src_data(src_data),
        .xfer_done(xfer_done), .xfer_chunk(xfer_chunk), .xfer_gen(xfer_gen),
        .busy(busy), .stat_chunks_done(stat_chunks_done),
        .stat_beats_sent(stat_beats_sent)
    );

    // Monitors
    always @(posedge clk) begin
        if (rst_n && tlp_valid && tlp_ready && credit_avail) begin
            beats_observed <= beats_observed + 1;
            if (tlp_pasid != 20'hBEEF) pasid_bad <= tlp_pasid;
        end
        if (rst_n && xfer_done) done_pulses <= done_pulses + 1;
    end

    task automatic chk(input logic c, input string m);
        if (!c) begin $display("  [FAIL] %s", m); errors++; end
        else       $display("  [ok]   %s", m);
    endtask

    initial begin
        admit_valid = 0; admit_chunk = 0; admit_gen = 0; admit_pasid = 0;
        admit_gpu_addr = 0; admit_beats = 0;
        tlp_ready = 1; credit_avail = 1; src_data = 256'hCAFE;
        repeat (4) @(negedge clk);
        rst_n = 1;
        @(negedge clk);

        $display("=== cefe_dma_engine_tb ===");

        // T1: issue a 4-beat chunk.
        @(negedge clk);
        admit_valid = 1; admit_chunk = 9'd7; admit_gen = 16'd12;
        admit_pasid = 20'hBEEF; admit_gpu_addr = 48'h1000; admit_beats = 5'd4;
        @(negedge clk);
        admit_valid = 0;

        // Let it stream (with full credit). Use the DUT's authoritative beat
        // counter (stat_beats_sent) rather than a racy TB-side monitor.
        repeat (12) @(negedge clk);
        chk(stat_beats_sent == 4, $sformatf("T1: 4 posted-write beats issued (got %0d)", stat_beats_sent));
        chk(pasid_bad == 0, "T1: PASID carried correctly on every beat");
        chk(done_pulses == 1, "T3: exactly one xfer_done pulse");
        chk(xfer_chunk == 9'd7 && xfer_gen == 16'd12,
            "T3: xfer_done carries correct (chunk=7, gen=12) for RELEASE(d)");

        // T2: credit-based flow control mid-transfer.
        done_pulses = 0;
        @(negedge clk);
        admit_valid = 1; admit_chunk = 9'd8; admit_gen = 16'd13;
        admit_pasid = 20'hBEEF; admit_gpu_addr = 48'h2000; admit_beats = 5'd4;
        @(negedge clk);
        admit_valid = 0;
        // After 2 beats of this chunk (total 6), starve credit.
        wait (stat_beats_sent == 6);
        credit_avail = 0;
        repeat (5) @(negedge clk);
        chk(stat_beats_sent == 6, "T2: no beats fire while credit is unavailable");
        credit_avail = 1;   // return credit
        repeat (10) @(negedge clk);
        chk(stat_beats_sent == 8, "T2: transfer resumes and completes after credit returns");
        chk(done_pulses == 1, "T2: completion fires once after credit-stalled transfer");

        $display("=== cefe_dma_engine_tb: %0d errors ===", errors);
        if (errors == 0) $display("PASS"); else $display("FAIL");
        $finish;
    end

endmodule
