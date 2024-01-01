// APEX MAC Array — 7-Wide Weighted Accumulator (Wallace Tree)
// Computes: score = Σ_{k=0}^{6} weight[k] × prediction[k]
//   - 7 parallel multipliers: 8-bit weight × 16-bit prediction → 24-bit
//   - 4-level 3:2 carry-save reduction (7-input Wallace tree) + 1 CPA
//   - Normalize (shift right 8) → 16-bit output score
//
// Note: a 7-operand Wallace tree built from 3:2 compressors requires 4
// reduction levels (not 3 as claimed in early drafts).  The 4-level tree
// here uses 5 CSA blocks.
//
// TIMING (honest, from asic/reports/timing.rpt Path 1 — structural estimate,
// NOT signoff STA): DFF clk-Q 92.9 + partial products 54.6 + Wallace/CSA
// 556.8 + 16b CPA 212.2 + setup 40.3 ≈ 956.9 ps at 7 nm TT. This is ~0.96 ns,
// NOT the 0.60 ns claimed in earlier drafts. Slack at 1 GHz is only ~43 ps
// before wire delay, so this is the functional critical path.
//
// Final adder: kept as the SystemVerilog `+` operator so the synthesis tool
// maps it to a technology-optimal prefix adder (Kogge-Stone / Brent-Kung)
// under the `set_max_delay` constraint in APEX_PIPELINE.sdc. Hand-coding a
// prefix network in RTL gives no guaranteed timing benefit here and risks a
// functional-equivalence bug, so it is intentionally left to synthesis.
//
// Hardware cost: 5 full-adder CSA blocks + 1 prefix CPA
//
// Target: ASAP7 7nm @ 1GHz
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_MAC_ARRAY (
    input  logic        clk,
    input  logic        rst_n,

    // Global pipeline freeze (backpressure / Case 2 stall). Holds the output
    // register so the scored descriptor is not lost during a stall cycle.
    input  logic        stall,

    // Expert predictions (from 7 banks, registered in S3)
    input  logic [15:0] pred_0,
    input  logic [15:0] pred_1,
    input  logic [15:0] pred_2,
    input  logic [15:0] pred_3,
    input  logic [15:0] pred_4,
    input  logic [15:0] pred_5,
    input  logic [15:0] pred_6,
    input  logic        pred_valid,

    // Weights (from weight register file, quasi-static within decode step)
    input  logic [7:0]  weight_0,
    input  logic [7:0]  weight_1,
    input  logic [7:0]  weight_2,
    input  logic [7:0]  weight_3,
    input  logic [7:0]  weight_4,
    input  logic [7:0]  weight_5,
    input  logic [7:0]  weight_6,

    // Chunk ID passthrough
    input  logic [8:0]  chunk_id_in,

    // Output (registered)
    output logic [15:0] score_out,
    output logic [8:0]  chunk_id_out,
    output logic        score_valid
);

    //=========================================================================
    // 3:2 Carry-Save Compressor (Full-Adder Array)
    // Reduces 3 input vectors to 2 output vectors (sum + carry) without
    // carry propagation. Carry output is left-shifted by 1 bit.
    //=========================================================================
    function automatic logic [26:0] csa_sum(
        input logic [26:0] a,
        input logic [26:0] b,
        input logic [26:0] c
    );
        csa_sum = a ^ b ^ c;
    endfunction

    function automatic logic [26:0] csa_carry(
        input logic [26:0] a,
        input logic [26:0] b,
        input logic [26:0] c
    );
        csa_carry = ((a & b) | (b & c) | (a & c)) << 1;
    endfunction

    //=========================================================================
    // Stage: Parallel Multiply (8b × 16b → 24b unsigned)
    //=========================================================================
    logic [23:0] prod [0:6];

    assign prod[0] = weight_0 * pred_0;
    assign prod[1] = weight_1 * pred_1;
    assign prod[2] = weight_2 * pred_2;
    assign prod[3] = weight_3 * pred_3;
    assign prod[4] = weight_4 * pred_4;
    assign prod[5] = weight_5 * pred_5;
    assign prod[6] = weight_6 * pred_6;

    //=========================================================================
    // Wallace Tree Reduction via 3:2 CSA Compressors (4 levels)
    //
    // Level 1: 7 inputs → 5 values
    //   CSA_1a: prod[0], prod[1], prod[2] → s1a, c1a
    //   CSA_1b: prod[3], prod[4], prod[5] → s1b, c1b
    //   Passthrough: prod[6]
    //   Result: s1a, c1a, s1b, c1b, prod[6] = 5 values
    //
    // Level 2: 5 values → 4 values
    //   CSA_2a: s1a, c1a, s1b → s2a, c2a
    //   Passthrough: c1b, prod[6]
    //   Result: s2a, c2a, c1b, prod[6] = 4 values
    //
    // Level 3: 4 values → 3 values
    //   CSA_3a: s2a, c2a, c1b → s3a, c3a
    //   Passthrough: prod[6]
    //   Result: s3a, c3a, prod[6] = 3 values
    //
    // Level 4: 3 values → 2 values
    //   CSA_4a: s3a, c3a, prod[6] → s4a, c4a
    //   Result: s4a, c4a = 2 values → feed to final CPA
    //=========================================================================

    // Zero-extend products to 27 bits for headroom
    logic [26:0] p [0:6];

    assign p[0] = {3'b0, prod[0]};
    assign p[1] = {3'b0, prod[1]};
    assign p[2] = {3'b0, prod[2]};
    assign p[3] = {3'b0, prod[3]};
    assign p[4] = {3'b0, prod[4]};
    assign p[5] = {3'b0, prod[5]};
    assign p[6] = {3'b0, prod[6]};

    // --- Stage 1: 7 → 5 ---
    logic [26:0] s1a, c1a, s1b, c1b;

    assign s1a = csa_sum  (p[0], p[1], p[2]);
    assign c1a = csa_carry(p[0], p[1], p[2]);
    assign s1b = csa_sum  (p[3], p[4], p[5]);
    assign c1b = csa_carry(p[3], p[4], p[5]);
    // Passthrough: p[6]

    // --- Stage 2: 5 → 4 ---
    logic [26:0] s2a, c2a;

    assign s2a = csa_sum  (s1a, c1a, s1b);
    assign c2a = csa_carry(s1a, c1a, s1b);
    // Passthrough: c1b, p[6]

    // --- Stage 3: 4 → 3 ---
    logic [26:0] s3a, c3a;

    assign s3a = csa_sum  (s2a, c2a, c1b);
    assign c3a = csa_carry(s2a, c2a, c1b);
    // Passthrough: p[6]

    // --- Stage 4: 3 → 2 ---
    logic [26:0] s4a, c4a;

    assign s4a = csa_sum  (s3a, c3a, p[6]);
    assign c4a = csa_carry(s3a, c3a, p[6]);

    //=========================================================================
    // Final Carry-Propagate Adder (CPA) — the ONLY `+` operator
    // Merges the two remaining vectors into a single 27-bit result
    //=========================================================================
    logic [26:0] sum_final;

    assign sum_final = s4a + c4a;

    //=========================================================================
    // Normalize: shift right by 8 (weights sum to ~255, so divide by 256)
    // Result is 16-bit with saturation
    //=========================================================================
    logic [18:0] normalized;
    logic [15:0] score_comb;

    assign normalized = sum_final[26:8];
    assign score_comb = (normalized > 19'h0FFFF) ? 16'hFFFF : normalized[15:0];

    //=========================================================================
    // Output Register (pipeline stage boundary)
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            score_out    <= '0;
            chunk_id_out <= '0;
            score_valid  <= 1'b0;
        end else if (!stall) begin
            score_out    <= score_comb;
            chunk_id_out <= chunk_id_in;
            score_valid  <= pred_valid;
        end
    end

endmodule
