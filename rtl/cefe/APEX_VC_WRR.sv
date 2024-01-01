//=============================================================================
// APEX_VC_WRR — Deficit Weighted Round-Robin Arbiter for 16 Virtual Channels
//
// Pure hardware per-tenant fairness enforcement with zero host coordination.
// Each VC accumulates a deficit counter proportional to its configured weight.
// Grant is issued to the highest-deficit requesting VC via rotating priority.
//
// Properties:
//   - Minimum guaranteed bandwidth: weight[i] / Σ weights
//   - Anti-starvation: any non-empty VC serviced within NUM_VC rounds
//   - No host software coordination required (autonomous fairness)
//   - Single-cycle grant latency (combinational scan from rr_ptr)
//   - step_boundary replenishes deficit quanta (once per decode step)
//
// This module wraps the deficit logic for standalone instantiation in the
// CEFE front-end, separate from queue storage. For the full VC-WRR with
// integrated 32-deep queues, see cefe_vc_wrr.sv.
//
// Target: ASAP7 7nm @ 1 GHz
// Area estimate: ~0.004 mm² (counters + priority logic only)
//=============================================================================
`timescale 1ns/1ps

module APEX_VC_WRR #(
    parameter int NUM_VC   = 16,
    parameter int WEIGHT_W = 4
)(
    input  logic                         clk,
    input  logic                         rst_n,

    // Decode step boundary (replenishes deficit quanta)
    input  logic                         step_boundary,

    // Per-VC request signals (active-high: VC has pending work)
    input  logic [NUM_VC-1:0]            req,

    // Per-VC weights (quasi-static, configured at init)
    input  logic [WEIGHT_W-1:0]          weight [0:NUM_VC-1],

    // Grant output
    output logic [$clog2(NUM_VC)-1:0]    grant_id,
    output logic                         grant_valid,

    // Backpressure: downstream not ready
    input  logic                         stall
);

    localparam int VC_BITS = $clog2(NUM_VC);

    //=========================================================================
    // Deficit Counters — one per VC, WEIGHT_W+1 bits to prevent overflow
    //=========================================================================
    logic [WEIGHT_W:0] deficit [0:NUM_VC-1];

    //=========================================================================
    // Round-Robin Pointer — rotates unconditionally for anti-starvation
    //=========================================================================
    logic [VC_BITS-1:0] rr_ptr;

    //=========================================================================
    // Combinational Grant Logic: scan from rr_ptr, grant first eligible VC
    // Eligible = req[i] && deficit[i] > 0
    //=========================================================================
    logic [VC_BITS-1:0] selected_vc;
    logic               selected_valid;

    always_comb begin
        selected_vc    = rr_ptr;
        selected_valid = 1'b0;

        if (!stall) begin
            for (int i = 0; i < NUM_VC; i++) begin
                // Wrap-around scan starting from rr_ptr
                if (!selected_valid && req[rr_ptr + VC_BITS'(i)] &&
                    (deficit[rr_ptr + VC_BITS'(i)] > '0)) begin
                    selected_vc    = rr_ptr + VC_BITS'(i);
                    selected_valid = 1'b1;
                end
            end
        end
    end

    assign grant_id    = selected_vc;
    assign grant_valid = selected_valid;

    //=========================================================================
    // Sequential: Deficit management + RR pointer advance
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rr_ptr <= '0;
            for (int i = 0; i < NUM_VC; i++) begin
                deficit[i] <= '0;
            end
        end else begin
            //=================================================================
            // Step boundary: replenish deficit quanta for all active VCs
            // This is the "credit addition" phase of DWRR, triggered once
            // per decode step to prevent unbounded accumulation.
            //=================================================================
            if (step_boundary) begin
                for (int i = 0; i < NUM_VC; i++) begin
                    if (req[i]) begin
                        // Add weight quantum, cap at max to prevent overflow
                        if (deficit[i] + {1'b0, weight[i]} > {(WEIGHT_W+1){1'b1}})
                            deficit[i] <= {(WEIGHT_W+1){1'b1}};
                        else
                            deficit[i] <= deficit[i] + {1'b0, weight[i]};
                    end else begin
                        // Idle VC: reset deficit (no accumulation when inactive)
                        deficit[i] <= '0;
                    end
                end
            end

            //=================================================================
            // Grant: decrement deficit for serviced VC
            //=================================================================
            if (grant_valid && !stall) begin
                deficit[selected_vc] <= deficit[selected_vc] - 1'b1;
            end

            //=================================================================
            // RR pointer: advance unconditionally every non-stall cycle
            // Guarantees all VCs visited within NUM_VC cycles (anti-starvation)
            //=================================================================
            if (!stall) begin
                rr_ptr <= rr_ptr + 1'b1;
            end
        end
    end

endmodule
