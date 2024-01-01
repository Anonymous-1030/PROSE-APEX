//=============================================================================
// APEX_SEA — Stochastic Exploration and Adaptation
//
// Injects exploration probes into the descriptor stream when coverage is low.
// Decays exploration rate (epsilon) when coverage exceeds configurable threshold.
//
// Mechanisms:
//   1. 16-bit Galois LFSR for pseudo-random number generation
//   2. 512-bit coverage bitmap tracking unique chunks seen per decode step
//   3. Epsilon decay: holds at EPSILON_INIT when coverage < threshold,
//      right-shifts (halves) on each step_boundary once coverage is met
//   4. Probe injection: if lfsr[7:0] < epsilon on desc_valid → probe
//
// Target: ASAP7 7nm @ 1GHz
// iverilog-compatible: no variable bit-selects in sensitivity lists
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_SEA #(
    parameter [15:0] COVERAGE_THRESH = 16'd39322,  // ~0.6 × 65535 (Q0.16)
    parameter  [7:0] EPSILON_INIT    = 8'd26,      // ~0.1 × 255
    parameter        POOL_SIZE       = apex_pkg::NUM_CHUNKS
)(
    input  logic        clk,
    input  logic        rst_n,

    input  logic        desc_valid,
    input  logic [8:0]  desc_chunk_id,
    input  logic        step_boundary,
    input  logic        pipe_idle,        // ~cmd_valid & ~pipe_stall: pipeline has no real work
    input  logic        enable,           // global SEA probe enable (0 = deterministic mode)

    output logic        probe_inject,
    output logic [8:0]  probe_chunk_id,   // LFSR-derived chunk ID for probe injection
    output logic [7:0]  sea_epsilon,
    output logic [15:0] sea_coverage
);

    localparam COV_CNT_W = 10;

    //=========================================================================
    // 16-bit Galois LFSR — polynomial x^16+x^14+x^13+x^11+1
    //=========================================================================
    reg [15:0] lfsr_reg;
    wire [15:0] lfsr_next;

    assign lfsr_next = lfsr_reg[0] ? ((lfsr_reg >> 1) ^ 16'hB400)
                                   : (lfsr_reg >> 1);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            lfsr_reg <= 16'hACE1;
        else if (desc_valid)
            lfsr_reg <= lfsr_next;
    end

    //=========================================================================
    // Coverage tracker — 512-bit bitmap + incremental counter
    // Uses a decoded one-hot mask to avoid variable bit-selects (iverilog)
    //=========================================================================
    reg [POOL_SIZE-1:0]  coverage_bits;
    reg [COV_CNT_W-1:0]  coverage_count;

    // Decode chunk_id to one-hot mask
    wire [POOL_SIZE-1:0] chunk_mask = ({{(POOL_SIZE-1){1'b0}}, 1'b1}) << desc_chunk_id;

    // Check if bit was already set (OR-reduction of mask AND existing bits)
    wire bit_already_set = |(coverage_bits & chunk_mask);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            coverage_bits  <= {POOL_SIZE{1'b0}};
            coverage_count <= {COV_CNT_W{1'b0}};
        end else if (step_boundary) begin
            coverage_bits  <= {POOL_SIZE{1'b0}};
            coverage_count <= {COV_CNT_W{1'b0}};
        end else if (desc_valid) begin
            coverage_bits <= coverage_bits | chunk_mask;
            if (!bit_already_set)
                coverage_count <= coverage_count + 1'b1;
        end
    end

    //=========================================================================
    // Coverage output (Q0.16): coverage_count / 512 * 65535 ≈ count << 7
    //=========================================================================
    assign sea_coverage = (coverage_count >= 10'd512) ? 16'hFFFF
                         : {coverage_count[8:0], 7'b0};

    //=========================================================================
    // Epsilon logic — decays on step_boundary once coverage exceeds threshold
    //=========================================================================
    reg [7:0] epsilon_reg;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            epsilon_reg <= EPSILON_INIT;
        else if (step_boundary) begin
            if (sea_coverage >= COVERAGE_THRESH)
                epsilon_reg <= epsilon_reg >> 1;
            else
                epsilon_reg <= EPSILON_INIT;
        end
    end

    assign sea_epsilon = epsilon_reg;

    //=========================================================================
    // Probe injection: fires ONLY when pipeline is idle (no real descriptor)
    // and LFSR output is below epsilon threshold. This eliminates unnecessary
    // toggling when real descriptors are being processed.
    //=========================================================================
    assign probe_inject = enable & pipe_idle & (lfsr_reg[7:0] < epsilon_reg);

    //=========================================================================
    // Probe chunk ID: use LFSR bits [8:0] as pseudo-random target chunk
    // Provides unbiased exploration of unobserved chunk address space
    //=========================================================================
    assign probe_chunk_id = lfsr_reg[8:0];

endmodule
