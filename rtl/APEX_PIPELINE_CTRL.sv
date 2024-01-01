//=============================================================================
// APEX Pipeline Controller
// Manages pipeline valid/stall signals and the reject bypass path.
//
// Key responsibilities:
//   - Track valid bits through 9 pipeline stages
//   - Implement reject bypass: PCM reject → skip S3-S6 → null-complete
//   - Handle backpressure from DMA and completion ring interfaces
//   - Generate stall signals when downstream is blocked
//
// Target: ASAP7 7nm @ 1GHz
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_PIPELINE_CTRL (
    input  logic        clk,
    input  logic        rst_n,

    // Pipeline stage valid signals (from each stage)
    input  logic        s1_valid,       // Descriptor dequeued
    input  logic        s2_valid,       // PCM output valid
    input  logic        s2_reject,      // PCM rejected this descriptor
    input  logic        s3_valid,       // Expert bank read done
    input  logic        s4_valid,       // MAC score done
    input  logic        s5_valid,       // Heap decision done
    input  logic        s6_valid,       // Weight update done (overlapped)

    // Backpressure inputs
    input  logic        dma_ready,      // DMA interface can accept
    input  logic        cpl_ready,      // Completion ring can accept
    input  logic        case2_stall,    // S5 cross-zone replace in progress (1 cyc)

    // Control outputs
    output logic        pipe_stall,     // Front-of-pipe freeze (backpressure + Case 2)
    output logic        drain_stall,    // Backpressure only (gates S7 completion/DMA)
    output logic        reject_bypass,  // Activate reject bypass path
    output logic        s7_issue_dma,   // S7: issue DMA descriptor
    output logic        s7_null_cpl,    // S7: write null completion

    // Reject bypass data (direct from S2 to completion)
    output logic        bypass_cpl_valid,
    output logic [8:0]  bypass_chunk_id,

    // From PCM (for bypass)
    input  logic [8:0]  pcm_chunk_id
);

    //=========================================================================
    // Backpressure logic
    //=========================================================================
    // drain_stall: downstream (DMA / completion ring) cannot accept. This must
    // gate the S7 completion/DMA registers.
    assign drain_stall = (s5_valid && !dma_ready) || (s2_reject && !cpl_ready);

    // pipe_stall: freeze the FRONT of the pipe (S1-S5a). Includes drain_stall
    // plus the 1-cycle Case 2 stall. case2_stall must NOT gate S7 (the
    // committing Case 2 descriptor still needs to complete this cycle), so it
    // is deliberately excluded from drain_stall.
    assign pipe_stall = drain_stall || case2_stall;

    //=========================================================================
    // Reject bypass
    // When PCM rejects, the descriptor skips S3-S6 and goes directly to
    // null-completion. This is combinational from S2 output.
    //=========================================================================
    assign reject_bypass = s2_valid & s2_reject;

    // Bypass completion (directly from S2b output, same cycle)
    assign bypass_cpl_valid = reject_bypass & cpl_ready & ~pipe_stall;
    assign bypass_chunk_id  = pcm_chunk_id;

    //=========================================================================
    // S7 control signals
    // Normal path: after S5/S6, issue DMA for admitted descriptors
    //=========================================================================
    assign s7_issue_dma = s5_valid & ~pipe_stall & dma_ready;
    assign s7_null_cpl  = reject_bypass;  // Handled by bypass path

endmodule
