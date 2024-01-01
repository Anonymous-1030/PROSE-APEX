//=============================================================================
// APEX Expert Bank — 512×16-bit DFF Register File (Read-First)
// One of 7 parallel expert prediction banks in the APEX scoring pipeline.
// Single-cycle read, single-cycle write (1R1W).
//
// STRUCTURAL CAUSAL BOUNDARY (two-level enforcement):
//   1. Cycle granularity: Read and write are in separate always_ff blocks
//      enforcing READ-FIRST semantics. A scoring read at cycle T captures the
//      committed state from cycle T-1; a feedback write at cycle T cannot
//      corrupt the read data captured on the same clock edge.
//   2. Step granularity: the parent (APEX_PIPELINE) drives wr_en only when
//      pipeline_idle is asserted (fb_wr_en_gated = fb_valid & pipeline_idle),
//      so a step-t attention write cannot land while any step-t descriptor is
//      still in flight through S1-S7. This closes the cross-cycle leak where a
//      mid-step feedback write could otherwise be read by a later S3 read in
//      the SAME decode step.
//   Together these guarantee the APEX-Core2 scorer operates strictly on
//   committed historical (t-1) predictions — no current-step attention leakage.
//
// Target: ASAP7 7nm @ 1GHz
// Expected Area: ~10,650 µm² per bank (512 × 16 × 1.3 µm²/bit)
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_EXPERT_BANK (
    input  logic        clk,
    input  logic        rst_n,

    // Read port (for scoring path — S3)
    input  logic [8:0]  rd_addr,
    input  logic        rd_en,
    output logic [15:0] rd_data,

    // Write port (for feedback update — async from scoring)
    input  logic [8:0]  wr_addr,
    input  logic [15:0] wr_data,
    input  logic        wr_en
);

    localparam int NUM_ENTRIES = apex_pkg::NUM_CHUNKS;
    localparam int ADDR_WIDTH = apex_pkg::ID_W;
    localparam int DATA_WIDTH = apex_pkg::SCORE_W;

    // DFF-based register file — force register implementation, no SRAM inference
    // (* ram_style = "register" *)        // Vivado: prevent BRAM inference
    // (* rw_addr_collision = "no" *)      // DC: explicit no-collision semantics
    (* ram_style = "register", rw_addr_collision = "no" *)
    logic [DATA_WIDTH-1:0] regfile [0:NUM_ENTRIES-1];

    //=========================================================================
    // READ PORT — Strict Read-First (captures committed state t-1)
    // Structural causal boundary: Read captures committed state t-1 before
    // write of state t. Even if rd_addr == wr_addr on the same clock edge,
    // the read returns the OLD value (pre-write).
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_data <= '0;
        end else if (rd_en) begin
            rd_data <= regfile[rd_addr];
        end
    end

    //=========================================================================
    // WRITE PORT — Independent from read, cannot corrupt same-cycle reads
    // Feedback from GPU writes new attention mass; visible only on NEXT read.
    //=========================================================================
    always_ff @(posedge clk) begin
        if (wr_en) begin
            regfile[wr_addr] <= wr_data;
        end
    end

endmodule
