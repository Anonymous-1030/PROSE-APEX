//=============================================================================
// APEX Payload Commitment Mechanism (PCM)
// Validation-before-visibility gate (paper Section IV / Fig. system topology).
// 2-cycle pipelined validation of promotion descriptors.
//   Cycle 1 (S2a): Read stored epoch + residency bit for chunk_id
//   Cycle 2 (S2b): Compare epoch/namespace, check residency -> pass/reject
//
// A descriptor that fails epoch, namespace, or residency checks retires via a
// null completion (no payload moves); only validated chunks become visible.
//
// Reject path: S1(1) + S2a(1) + S2b(1) = 3 cycles to null-complete
//
// Target: ASAP7 7nm @ 1GHz
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_PCM (
    input  logic        clk,
    input  logic        rst_n,

    // Global pipeline freeze (backpressure / Case 2 stall). When high, the two
    // validation stages hold their contents so no descriptor is lost.
    input  logic        stall,

    // From S1 (descriptor dequeue)
    input  logic [8:0]  desc_chunk_id,
    input  logic [15:0] desc_epoch,
    input  logic [7:0]  desc_namespace,
    input  logic        desc_valid,

    // Configuration (quasi-static, set per decode step)
    input  logic [15:0] cfg_current_epoch,
    input  logic [7:0]  cfg_current_namespace,

    // Output (after 2 cycles)
    output logic        pcm_pass,
    output logic        pcm_reject,
    output logic        pcm_valid,
    output logic [8:0]  pcm_chunk_id_out,

    // Residency update interface (from DMA completion / eviction)
    input  logic [8:0]  res_set_id,
    input  logic        res_set_valid,
    input  logic [8:0]  res_clear_id,
    input  logic        res_clear_valid
);

    localparam int NUM_CHUNKS = apex_pkg::NUM_CHUNKS;

    //=========================================================================
    // State: Residency bit vector (512 bits)
    //=========================================================================
    logic [NUM_CHUNKS-1:0] residency_bits;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            residency_bits <= '0;
        end else begin
            if (res_set_valid)
                residency_bits[res_set_id] <= 1'b1;
            if (res_clear_valid)
                residency_bits[res_clear_id] <= 1'b0;
        end
    end

    //=========================================================================
    // Pipeline Stage 1 (S2a): Read residency + latch descriptor fields
    //=========================================================================
    logic        s1_valid;
    logic [8:0]  s1_chunk_id;
    logic [15:0] s1_epoch;
    logic [7:0]  s1_namespace;
    logic        s1_resident;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s1_valid <= 1'b0;
        end else if (!stall) begin
            s1_valid     <= desc_valid;
            s1_chunk_id  <= desc_chunk_id;
            s1_epoch     <= desc_epoch;
            s1_namespace <= desc_namespace;
            s1_resident  <= residency_bits[desc_chunk_id];
        end
    end

    //=========================================================================
    // Pipeline Stage 2 (S2b): Compare + decision
    //=========================================================================
    logic epoch_match;
    logic namespace_match;
    logic not_resident;
    logic all_pass;

    assign epoch_match     = (s1_epoch == cfg_current_epoch);
    assign namespace_match = (s1_namespace == cfg_current_namespace);
    assign not_resident    = ~s1_resident;
    assign all_pass        = epoch_match & namespace_match & not_resident;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pcm_valid  <= 1'b0;
            pcm_pass   <= 1'b0;
            pcm_reject <= 1'b0;
        end else if (!stall) begin
            pcm_valid        <= s1_valid;
            pcm_pass         <= s1_valid & all_pass;
            pcm_reject       <= s1_valid & ~all_pass;
            pcm_chunk_id_out <= s1_chunk_id;
        end
    end

endmodule
