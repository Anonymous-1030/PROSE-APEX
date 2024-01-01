//=============================================================================
// APEX_LOSS_COMPUTE — Hedge Weight Update Loss Computation
//
// Computes per-expert quantized loss for the APEX weight update stage.
// On admission, stores the expert predictions in a shadow register file
// indexed by chunk_id. When GPU feedback arrives, retrieves stored
// predictions and compares against observed attention mass to produce
// a 3-bit quantized loss per expert.
//
// Pipeline: 2-stage (fb_valid → read shadow RF → compute delta + quantize → output)
//   Stage 1: Register fb_chunk_id, read shadow_rf (registered output)
//   Stage 2: Compute |predicted - actual| combinationally, quantize, register output
//
// Shadow register file: 512 entries × (NUM_EXPERTS × 16-bit)
//
// Quantization thresholds (unsigned 16-bit delta):
//   delta > 0x6000 → 7
//   delta > 0x4000 → 5
//   delta > 0x2000 → 3
//   delta > 0x1000 → 1
//   else           → 0
//
// Port arrays are flattened to packed vectors for iverilog cross-module
// compatibility. Bit ordering: expert[N-1] is MSB, expert[0] is LSB.
//
// Target: ASAP7 7nm @ 1GHz
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_LOSS_COMPUTE #(
    parameter NUM_EXPERTS = apex_pkg::NUM_EXPERTS,
    parameter SCORE_W     = apex_pkg::SCORE_W,
    parameter ID_W        = apex_pkg::ID_W
)(
    input  logic                              clk,
    input  logic                              rst_n,

    // Admission event (from heap admit decision)
    input  logic                              admit_valid,
    input  logic [ID_W-1:0]                   admit_chunk_id,
    input  logic [NUM_EXPERTS*SCORE_W-1:0]    admit_expert_preds_flat,

    // Feedback (from GPU runtime)
    input  logic                              fb_valid,
    input  logic [ID_W-1:0]                   fb_chunk_id,
    input  logic [SCORE_W-1:0]                fb_attention_mass,

    // Output — flattened loss_q (3 bits × NUM_EXPERTS)
    output logic [NUM_EXPERTS*3-1:0]          loss_q_flat,
    output logic                              loss_valid
);

    localparam DEPTH = 1 << ID_W;  // 512

    //=========================================================================
    // Unpack flat input vectors into arrays
    //=========================================================================
    logic [SCORE_W-1:0] admit_preds [0:NUM_EXPERTS-1];
    integer ui;
    always_comb begin
        for (ui = 0; ui < NUM_EXPERTS; ui = ui + 1)
            admit_preds[ui] = admit_expert_preds_flat[ui*SCORE_W +: SCORE_W];
    end

    //=========================================================================
    // Shadow Register File: 512 entries × (NUM_EXPERTS × SCORE_W bits)
    //=========================================================================
    reg [SCORE_W-1:0] shadow_rf [0:DEPTH-1][0:NUM_EXPERTS-1];

    // Write port: on admission, store predictions
    integer wi;
    always @(posedge clk) begin
        if (admit_valid) begin
            for (wi = 0; wi < NUM_EXPERTS; wi = wi + 1)
                shadow_rf[admit_chunk_id][wi] <= admit_preds[wi];
        end
    end

    //=========================================================================
    // Pipeline Stage 1: Capture feedback, read shadow RF
    //=========================================================================
    reg                 s1_valid;
    reg [SCORE_W-1:0]   s1_preds [0:NUM_EXPERTS-1];
    reg [SCORE_W-1:0]   s1_attention_mass;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            s1_valid <= 1'b0;
        else
            s1_valid <= fb_valid;
    end

    integer ri;
    always @(posedge clk) begin
        if (fb_valid) begin
            for (ri = 0; ri < NUM_EXPERTS; ri = ri + 1)
                s1_preds[ri] <= shadow_rf[fb_chunk_id][ri];
            s1_attention_mass <= fb_attention_mass;
        end
    end

    //=========================================================================
    // Pipeline Stage 2: Compute delta, quantize, register output
    //=========================================================================
    reg [SCORE_W-1:0] delta [0:NUM_EXPERTS-1];
    reg [2:0]         loss_comb [0:NUM_EXPERTS-1];

    integer ci;
    always @(*) begin
        for (ci = 0; ci < NUM_EXPERTS; ci = ci + 1) begin
            if (s1_preds[ci] >= s1_attention_mass)
                delta[ci] = s1_preds[ci] - s1_attention_mass;
            else
                delta[ci] = s1_attention_mass - s1_preds[ci];

            if (delta[ci] > 16'h6000)
                loss_comb[ci] = 3'd7;
            else if (delta[ci] > 16'h4000)
                loss_comb[ci] = 3'd5;
            else if (delta[ci] > 16'h2000)
                loss_comb[ci] = 3'd3;
            else if (delta[ci] > 16'h1000)
                loss_comb[ci] = 3'd1;
            else
                loss_comb[ci] = 3'd0;
        end
    end

    // Output register stage
    reg [2:0] loss_q_reg [0:NUM_EXPERTS-1];
    reg       loss_valid_reg;

    integer oi;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            loss_valid_reg <= 1'b0;
            for (oi = 0; oi < NUM_EXPERTS; oi = oi + 1)
                loss_q_reg[oi] <= 3'd0;
        end else begin
            loss_valid_reg <= s1_valid;
            if (s1_valid) begin
                for (oi = 0; oi < NUM_EXPERTS; oi = oi + 1)
                    loss_q_reg[oi] <= loss_comb[oi];
            end
        end
    end

    assign loss_valid = loss_valid_reg;

    // Pack output into flat vector
    integer pi;
    always_comb begin
        for (pi = 0; pi < NUM_EXPERTS; pi = pi + 1)
            loss_q_flat[pi*3 +: 3] = loss_q_reg[pi];
    end

endmodule
