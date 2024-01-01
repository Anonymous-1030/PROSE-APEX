//=============================================================================
// APEX Dual-Zone Exact Top-K Network
//
// Implements the O(1)-depth streaming top-K selection from the paper:
//   K=25 entries split into:
//     - Eviction Zone (EZ): 7 entries [0..6], organized as a min-heap (depth 2)
//       Root h_score[0] = ez_min = global minimum of all 25 retained entries
//     - Safe Zone (SZ): 18 entries [0..17], flat register array
//       safe_min tracked via a balanced 17-comparator min-tree (5 levels)
//
//   Three-branch admission rule (when count == 25):
//     Case 0 (reject):           x <= ez_min  -> no state change
//     Case 1 (EZ-local replace): ez_min < x <= safe_min -> evict ez_min,
//                                 insert x at root, 2-level min-sift-down
//     Case 2 (cross-zone):       x > safe_min -> evict ez_min, demote safe_min
//                                 into EZ root, sift-down; write x into SZ
//                                 at safe_min_idx; recompute safe_min
//
//   Case 2 single-cycle stall (paper §5.3, restored):
//     A Case 2 replacement mutates the Safe Zone and therefore invalidates
//     safe_min for the *next* descriptor. To avoid classifying descriptor
//     t+1 against the stale safe_min of step t, the FSM asserts case2_stall
//     for exactly one cycle on a Case 2 event; APEX_PIPELINE feeds this into
//     pipe_stall so S1-S4 freeze for that cycle (a 1-cycle bubble at S5a).
//     The speculative forwarding min-tree (sz_min_fwd) is what makes ONE
//     cycle sufficient: it computes the post-replacement safe_min in the same
//     cycle the replacement is committed, so safe_min is fresh on the cycle
//     the stalled descriptor is finally classified. Without forwarding this
//     would require a 2-cycle stall (write then re-min). The forwarding tree
//     is thus retained deliberately; it is NOT the eliminated-stall hack.
//
//   Invariant (Dual-Zone Exact Top-K):
//     1. ez_min = min(EZ) = min(all 25 retained)
//     2. forall e in EZ, s in SZ: e <= s  (max(EZ) <= safe_min)
//     3. safe_min is exact and fresh before each admission decision
//
//   TIMING NOTE (honest): the forwarding min-tree (sz_min_fwd) is a ~1650 ps
//   combinational path (see asic/reports/timing.rpt Path 3) that is used on
//   the admission cycle, so it CANNOT be declared multicycle. Closing it at
//   1 GHz requires either pipelining the SZ tree (split at level 3) or a
//   slower clock; this is flagged, not resolved, here. The primary tree
//   (sz_min_comb, idle-refresh only) legitimately takes the 2-cycle
//   multicycle constraint in the SDC.
//
//   Hardware cost: 17 primary SZ min-tree comparators + 17 speculative
//   forwarding comparators + 2 classification comparators + 9 EZ sift
//   comparators = 45 total.  The paper's "19 incremental comparators"
//   refers to the primary tree + classification overhead over a baseline
//   sequential heap.
//
// Target: ASAP7 7nm @ 1GHz
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_TOPK_HEAP #(
    parameter int K              = apex_pkg::K_ENTRIES,
    parameter int EZ_SIZE        = apex_pkg::EZ_SIZE,
    parameter int SZ_SIZE        = apex_pkg::SZ_SIZE,
    parameter int SCORE_W        = apex_pkg::SCORE_W,
    parameter int ID_W           = apex_pkg::ID_W,
    parameter int BURST_THRESH   = 12
)(
    input  logic                clk,
    input  logic                rst_n,

    input  logic [SCORE_W-1:0]  new_score,
    input  logic [ID_W-1:0]     new_chunk_id,
    input  logic                new_valid,

    // Upstream freeze request. When high, S5a inserts a bubble instead of
    // latching a new descriptor (used for the Case 2 safe_min-update stall and
    // for downstream backpressure). The producer (MAC) must hold its output
    // stable for the same cycle so the descriptor is not lost.
    input  logic                hold,

    output logic                admitted,
    output logic                admitted_valid,

    // Asserted for exactly one cycle while a Case 2 (cross-zone) replacement is
    // executing and safe_min is being updated. Fed back into pipe_stall so the
    // following descriptor cannot be classified against the stale safe_min.
    output logic                case2_stall,

    input  logic                readout_start,
    output logic [ID_W-1:0]     readout_chunk_id,
    output logic [SCORE_W-1:0]  readout_score,
    output logic                readout_valid,
    output logic                readout_done,

    input  logic                flush,
    output logic [4:0]          heap_count,
    output logic                heap_idle
);

    //=========================================================================
    // Heap State Machine: FILL -> HEAPIFY -> ADMIT
    //=========================================================================
    localparam [2:0] ST_FILL      = 3'd0;
    localparam [2:0] ST_HFY_NODE2 = 3'd1;
    localparam [2:0] ST_HFY_NODE1 = 3'd2;
    localparam [2:0] ST_HFY_NODE0 = 3'd3;
    localparam [2:0] ST_HFY_NODE0_CONT = 3'd4;
    localparam [2:0] ST_ADMIT     = 3'd5;

    reg [2:0] heap_state;

    //=========================================================================
    // Storage: Eviction Zone (EZ) min-heap [0..6] + Safe Zone (SZ) [0..17]
    //=========================================================================
    reg [SCORE_W-1:0] ez_score [0:EZ_SIZE-1];
    reg [ID_W-1:0]    ez_id    [0:EZ_SIZE-1];
    reg [SCORE_W-1:0] sz_score [0:SZ_SIZE-1];
    reg [ID_W-1:0]    sz_id    [0:SZ_SIZE-1];
    reg [4:0]         count;

    assign heap_count = count;

    wire admission_ready = (heap_state == ST_FILL) || (heap_state == ST_ADMIT);

    //=========================================================================
    // EZ root = global minimum (ez_min)
    //=========================================================================
    wire [SCORE_W-1:0] ez_min = ez_score[0];

    //=========================================================================
    // Balanced 17-comparator min-tree for Safe Zone (5 levels)
    // Inputs: sz_score[0..17]
    // Output: sz_min_comb, sz_min_idx_comb
    //=========================================================================
    reg [SCORE_W-1:0] safe_min;
    reg [4:0]         safe_min_idx;

    wire [SCORE_W-1:0] sz_min_comb;
    wire [4:0]         sz_min_idx_comb;

    // Level 1: 9 comparators (18 -> 9)
    wire [SCORE_W-1:0] l1_min [0:8];
    wire [4:0]         l1_idx [0:8];

    assign l1_min[0] = (sz_score[0] <= sz_score[1]) ? sz_score[0] : sz_score[1];
    assign l1_idx[0] = (sz_score[0] <= sz_score[1]) ? 5'd0 : 5'd1;
    assign l1_min[1] = (sz_score[2] <= sz_score[3]) ? sz_score[2] : sz_score[3];
    assign l1_idx[1] = (sz_score[2] <= sz_score[3]) ? 5'd2 : 5'd3;
    assign l1_min[2] = (sz_score[4] <= sz_score[5]) ? sz_score[4] : sz_score[5];
    assign l1_idx[2] = (sz_score[4] <= sz_score[5]) ? 5'd4 : 5'd5;
    assign l1_min[3] = (sz_score[6] <= sz_score[7]) ? sz_score[6] : sz_score[7];
    assign l1_idx[3] = (sz_score[6] <= sz_score[7]) ? 5'd6 : 5'd7;
    assign l1_min[4] = (sz_score[8] <= sz_score[9]) ? sz_score[8] : sz_score[9];
    assign l1_idx[4] = (sz_score[8] <= sz_score[9]) ? 5'd8 : 5'd9;
    assign l1_min[5] = (sz_score[10] <= sz_score[11]) ? sz_score[10] : sz_score[11];
    assign l1_idx[5] = (sz_score[10] <= sz_score[11]) ? 5'd10 : 5'd11;
    assign l1_min[6] = (sz_score[12] <= sz_score[13]) ? sz_score[12] : sz_score[13];
    assign l1_idx[6] = (sz_score[12] <= sz_score[13]) ? 5'd12 : 5'd13;
    assign l1_min[7] = (sz_score[14] <= sz_score[15]) ? sz_score[14] : sz_score[15];
    assign l1_idx[7] = (sz_score[14] <= sz_score[15]) ? 5'd14 : 5'd15;
    assign l1_min[8] = (sz_score[16] <= sz_score[17]) ? sz_score[16] : sz_score[17];
    assign l1_idx[8] = (sz_score[16] <= sz_score[17]) ? 5'd16 : 5'd17;

    // Level 2: 4 comparators + 1 pass (9 -> 5)
    wire [SCORE_W-1:0] l2_min [0:4];
    wire [4:0]         l2_idx [0:4];

    assign l2_min[0] = (l1_min[0] <= l1_min[1]) ? l1_min[0] : l1_min[1];
    assign l2_idx[0] = (l1_min[0] <= l1_min[1]) ? l1_idx[0] : l1_idx[1];
    assign l2_min[1] = (l1_min[2] <= l1_min[3]) ? l1_min[2] : l1_min[3];
    assign l2_idx[1] = (l1_min[2] <= l1_min[3]) ? l1_idx[2] : l1_idx[3];
    assign l2_min[2] = (l1_min[4] <= l1_min[5]) ? l1_min[4] : l1_min[5];
    assign l2_idx[2] = (l1_min[4] <= l1_min[5]) ? l1_idx[4] : l1_idx[5];
    assign l2_min[3] = (l1_min[6] <= l1_min[7]) ? l1_min[6] : l1_min[7];
    assign l2_idx[3] = (l1_min[6] <= l1_min[7]) ? l1_idx[6] : l1_idx[7];
    assign l2_min[4] = l1_min[8];
    assign l2_idx[4] = l1_idx[8];

    // Level 3: 2 comparators + 1 pass (5 -> 3)
    wire [SCORE_W-1:0] l3_min [0:2];
    wire [4:0]         l3_idx [0:2];

    assign l3_min[0] = (l2_min[0] <= l2_min[1]) ? l2_min[0] : l2_min[1];
    assign l3_idx[0] = (l2_min[0] <= l2_min[1]) ? l2_idx[0] : l2_idx[1];
    assign l3_min[1] = (l2_min[2] <= l2_min[3]) ? l2_min[2] : l2_min[3];
    assign l3_idx[1] = (l2_min[2] <= l2_min[3]) ? l2_idx[2] : l2_idx[3];
    assign l3_min[2] = l2_min[4];
    assign l3_idx[2] = l2_idx[4];

    // Level 4: 1 comparator + 1 pass (3 -> 2)
    wire [SCORE_W-1:0] l4_min [0:1];
    wire [4:0]         l4_idx [0:1];

    assign l4_min[0] = (l3_min[0] <= l3_min[1]) ? l3_min[0] : l3_min[1];
    assign l4_idx[0] = (l3_min[0] <= l3_min[1]) ? l3_idx[0] : l3_idx[1];
    assign l4_min[1] = l3_min[2];
    assign l4_idx[1] = l3_idx[2];

    // Level 5: 1 comparator (2 -> 1)
    assign sz_min_comb = (l4_min[0] <= l4_min[1]) ? l4_min[0] : l4_min[1];
    assign sz_min_idx_comb = (l4_min[0] <= l4_min[1]) ? l4_idx[0] : l4_idx[1];

    //=========================================================================
    // Speculative safe_min forwarding for Case 2
    // Compute min of SZ with new_score written at safe_min_idx
    //=========================================================================
    wire [SCORE_W-1:0] sz_min_fwd;
    wire [4:0]         sz_min_idx_fwd;

    wire [SCORE_W-1:0] f_sz [0:SZ_SIZE-1];
    genvar gi;
    generate
        for (gi = 0; gi < SZ_SIZE; gi++) begin : gen_fwd_override
            // Use the latched S5a score, not the external new_score, because
            // the external score may already reflect the next descriptor while
            // the current cross-zone replacement is being applied.
            assign f_sz[gi] = (gi[4:0] == safe_min_idx) ? s5a_score : sz_score[gi];
        end
    endgenerate

    // Balanced min-tree over f_sz (same structure as primary tree)
    wire [SCORE_W-1:0] fl1_min [0:8];
    wire [4:0]         fl1_idx [0:8];

    assign fl1_min[0] = (f_sz[0] <= f_sz[1]) ? f_sz[0] : f_sz[1];
    assign fl1_idx[0] = (f_sz[0] <= f_sz[1]) ? 5'd0 : 5'd1;
    assign fl1_min[1] = (f_sz[2] <= f_sz[3]) ? f_sz[2] : f_sz[3];
    assign fl1_idx[1] = (f_sz[2] <= f_sz[3]) ? 5'd2 : 5'd3;
    assign fl1_min[2] = (f_sz[4] <= f_sz[5]) ? f_sz[4] : f_sz[5];
    assign fl1_idx[2] = (f_sz[4] <= f_sz[5]) ? 5'd4 : 5'd5;
    assign fl1_min[3] = (f_sz[6] <= f_sz[7]) ? f_sz[6] : f_sz[7];
    assign fl1_idx[3] = (f_sz[6] <= f_sz[7]) ? 5'd6 : 5'd7;
    assign fl1_min[4] = (f_sz[8] <= f_sz[9]) ? f_sz[8] : f_sz[9];
    assign fl1_idx[4] = (f_sz[8] <= f_sz[9]) ? 5'd8 : 5'd9;
    assign fl1_min[5] = (f_sz[10] <= f_sz[11]) ? f_sz[10] : f_sz[11];
    assign fl1_idx[5] = (f_sz[10] <= f_sz[11]) ? 5'd10 : 5'd11;
    assign fl1_min[6] = (f_sz[12] <= f_sz[13]) ? f_sz[12] : f_sz[13];
    assign fl1_idx[6] = (f_sz[12] <= f_sz[13]) ? 5'd12 : 5'd13;
    assign fl1_min[7] = (f_sz[14] <= f_sz[15]) ? f_sz[14] : f_sz[15];
    assign fl1_idx[7] = (f_sz[14] <= f_sz[15]) ? 5'd14 : 5'd15;
    assign fl1_min[8] = (f_sz[16] <= f_sz[17]) ? f_sz[16] : f_sz[17];
    assign fl1_idx[8] = (f_sz[16] <= f_sz[17]) ? 5'd16 : 5'd17;

    wire [SCORE_W-1:0] fl2_min [0:4];
    wire [4:0]         fl2_idx [0:4];

    assign fl2_min[0] = (fl1_min[0] <= fl1_min[1]) ? fl1_min[0] : fl1_min[1];
    assign fl2_idx[0] = (fl1_min[0] <= fl1_min[1]) ? fl1_idx[0] : fl1_idx[1];
    assign fl2_min[1] = (fl1_min[2] <= fl1_min[3]) ? fl1_min[2] : fl1_min[3];
    assign fl2_idx[1] = (fl1_min[2] <= fl1_min[3]) ? fl1_idx[2] : fl1_idx[3];
    assign fl2_min[2] = (fl1_min[4] <= fl1_min[5]) ? fl1_min[4] : fl1_min[5];
    assign fl2_idx[2] = (fl1_min[4] <= fl1_min[5]) ? fl1_idx[4] : fl1_idx[5];
    assign fl2_min[3] = (fl1_min[6] <= fl1_min[7]) ? fl1_min[6] : fl1_min[7];
    assign fl2_idx[3] = (fl1_min[6] <= fl1_min[7]) ? fl1_idx[6] : fl1_idx[7];
    assign fl2_min[4] = fl1_min[8];
    assign fl2_idx[4] = fl1_idx[8];

    wire [SCORE_W-1:0] fl3_min [0:2];
    wire [4:0]         fl3_idx [0:2];

    assign fl3_min[0] = (fl2_min[0] <= fl2_min[1]) ? fl2_min[0] : fl2_min[1];
    assign fl3_idx[0] = (fl2_min[0] <= fl2_min[1]) ? fl2_idx[0] : fl2_idx[1];
    assign fl3_min[1] = (fl2_min[2] <= fl2_min[3]) ? fl2_min[2] : fl2_min[3];
    assign fl3_idx[1] = (fl2_min[2] <= fl2_min[3]) ? fl2_idx[2] : fl2_idx[3];
    assign fl3_min[2] = fl2_min[4];
    assign fl3_idx[2] = fl2_idx[4];

    wire [SCORE_W-1:0] fl4_min [0:1];
    wire [4:0]         fl4_idx [0:1];

    assign fl4_min[0] = (fl3_min[0] <= fl3_min[1]) ? fl3_min[0] : fl3_min[1];
    assign fl4_idx[0] = (fl3_min[0] <= fl3_min[1]) ? fl3_idx[0] : fl3_idx[1];
    assign fl4_min[1] = fl3_min[2];
    assign fl4_idx[1] = fl3_idx[2];

    assign sz_min_fwd = (fl4_min[0] <= fl4_min[1]) ? fl4_min[0] : fl4_min[1];
    assign sz_min_idx_fwd = (fl4_min[0] <= fl4_min[1]) ? fl4_idx[0] : fl4_idx[1];

    //=========================================================================
    // Pipeline idle indicator
    //=========================================================================
    assign heap_idle = ~s5a_valid & ~rd_active &
                       ((heap_state == ST_FILL) || (heap_state == ST_ADMIT));

    //=========================================================================
    // Case 2 stall generation.
    // A cross-zone replacement is about to commit this cycle (s5a holds a
    // Case 2 descriptor in ADMIT). Assert case2_stall so APEX_PIPELINE freezes
    // S1-S4 for one cycle, guaranteeing the next descriptor is classified only
    // after safe_min has been refreshed (via sz_min_fwd, committed this cycle).
    //=========================================================================
    assign case2_stall = (heap_state == ST_ADMIT) & s5a_valid & (s5a_case == 2'b10);

    //=========================================================================
    // S5a: Classification stage
    // When `hold` is high we freeze: no new descriptor is latched and s5a_valid
    // is cleared (bubble). This implements the Case 2 single-cycle stall and
    // downstream backpressure without dropping the incoming descriptor (the
    // producer holds its output stable while hold is asserted).
    //=========================================================================
    reg               s5a_valid;
    reg [SCORE_W-1:0] s5a_score;
    reg [ID_W-1:0]    s5a_id;
    reg [1:0]         s5a_case;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n || flush) begin
            s5a_valid <= 1'b0;
            s5a_case  <= 2'b00;
        end else if (hold) begin
            // Freeze: insert a bubble at S5a, hold nothing new. The already
            // latched descriptor (if any) has been consumed this cycle by the
            // ST_ADMIT update; a bubble prevents re-classifying the next
            // descriptor against a stale safe_min.
            s5a_valid <= 1'b0;
            s5a_case  <= 2'b00;
        end else if (!admission_ready) begin
            s5a_valid <= 1'b0;
            s5a_case  <= 2'b00;
        end else begin
            s5a_valid <= new_valid;
            s5a_score <= new_score;
            s5a_id    <= new_chunk_id;
            if (new_valid) begin
                if (heap_state == ST_FILL) begin
                    s5a_case <= 2'b11;
                end else if (new_score <= ez_min) begin
                    s5a_case <= 2'b00;
                end else if (new_score <= safe_min) begin
                    s5a_case <= 2'b01;
                end else begin
                    s5a_case <= 2'b10;
                end
            end else begin
                s5a_case <= 2'b00;
            end
        end
    end

    //=========================================================================
    // S5b: Sift-down combinational network for EZ (depth 2)
    //=========================================================================
    wire [SCORE_W-1:0] sift_val = (s5a_case == 2'b10) ? safe_min : s5a_score;

    // Level 0: compare children of root
    wire l0_child1_smaller = (ez_score[1] <= ez_score[2]);
    wire l0_go_left  = l0_child1_smaller && (sift_val > ez_score[1]);
    wire l0_go_right = !l0_child1_smaller && (sift_val > ez_score[2]);

    // Level 1 via left child (node 1 -> children 3, 4)
    wire l1_child3_smaller = (ez_score[3] <= ez_score[4]);
    wire l1_go_left_via1   = l0_go_left && l1_child3_smaller && (sift_val > ez_score[3]);
    wire l1_go_right_via1  = l0_go_left && !l1_child3_smaller && (sift_val > ez_score[4]);

    // Level 1 via right child (node 2 -> children 5, 6)
    wire l1_child5_smaller = (ez_score[5] <= ez_score[6]);
    wire l1_go_left_via2   = l0_go_right && l1_child5_smaller && (sift_val > ez_score[5]);
    wire l1_go_right_via2  = l0_go_right && !l1_child5_smaller && (sift_val > ez_score[6]);

    //=========================================================================
    // Helper wires for heapify sift-down
    //=========================================================================
    wire [SCORE_W-1:0] hfy_node2_val  = ez_score[2];
    wire [SCORE_W-1:0] hfy_node2_lc   = ez_score[5];
    wire [SCORE_W-1:0] hfy_node2_rc   = ez_score[6];
    wire               hfy_node2_lsm  = (hfy_node2_lc <= hfy_node2_rc);
    wire [SCORE_W-1:0] hfy_node2_minv = hfy_node2_lsm ? hfy_node2_lc : hfy_node2_rc;
    wire [2:0]         hfy_node2_minc = hfy_node2_lsm ? 3'd5 : 3'd6;
    wire               hfy_node2_swap = (hfy_node2_minv < hfy_node2_val);

    wire [SCORE_W-1:0] hfy_node1_val  = ez_score[1];
    wire [SCORE_W-1:0] hfy_node1_lc   = ez_score[3];
    wire [SCORE_W-1:0] hfy_node1_rc   = ez_score[4];
    wire               hfy_node1_lsm  = (hfy_node1_lc <= hfy_node1_rc);
    wire [SCORE_W-1:0] hfy_node1_minv = hfy_node1_lsm ? hfy_node1_lc : hfy_node1_rc;
    wire [2:0]         hfy_node1_minc = hfy_node1_lsm ? 3'd3 : 3'd4;
    wire               hfy_node1_swap = (hfy_node1_minv < hfy_node1_val);

    wire [SCORE_W-1:0] hfy_node0_val  = ez_score[0];
    wire [SCORE_W-1:0] hfy_node0_lc   = ez_score[1];
    wire [SCORE_W-1:0] hfy_node0_rc   = ez_score[2];
    wire               hfy_node0_lsm  = (hfy_node0_lc <= hfy_node0_rc);
    wire [SCORE_W-1:0] hfy_node0_minv = hfy_node0_lsm ? hfy_node0_lc : hfy_node0_rc;
    wire [2:0]         hfy_node0_minc = hfy_node0_lsm ? 3'd1 : 3'd2;
    wire               hfy_node0_swap = (hfy_node0_minv < hfy_node0_val);

    // Continuation: after swapping node 0 with child, the demoted value may
    // need to sift down one more level.  We register the demoted value and
    // target child index at the ST_HFY_NODE0 clock edge to avoid reading
    // stale combinational signals derived from the pre-swap ez_score.
    reg [SCORE_W-1:0] hfy_cont_val_r;
    reg [2:0]         hfy_cont_idx_r;

    wire [SCORE_W-1:0] hfy_cont_lc   = (hfy_cont_idx_r == 3'd1) ? ez_score[3] : ez_score[5];
    wire [SCORE_W-1:0] hfy_cont_rc   = (hfy_cont_idx_r == 3'd1) ? ez_score[4] : ez_score[6];
    wire               hfy_cont_lsm  = (hfy_cont_lc <= hfy_cont_rc);
    wire [SCORE_W-1:0] hfy_cont_minv = hfy_cont_lsm ? hfy_cont_lc : hfy_cont_rc;
    wire [2:0]         hfy_cont_minc = hfy_cont_lsm ?
                                        ((hfy_cont_idx_r == 3'd1) ? 3'd3 : 3'd5) :
                                        ((hfy_cont_idx_r == 3'd1) ? 3'd4 : 3'd6);
    wire               hfy_cont_swap = (hfy_cont_minv < hfy_cont_val_r);

    //=========================================================================
    // State update
    //=========================================================================
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n || flush) begin
            count           <= 5'd0;
            admitted        <= 1'b0;
            admitted_valid  <= 1'b0;
            heap_state      <= ST_FILL;
            safe_min        <= {SCORE_W{1'b0}};
            safe_min_idx    <= 5'd0;
            hfy_cont_val_r  <= {SCORE_W{1'b0}};
            hfy_cont_idx_r  <= 3'd0;
            for (i = 0; i < EZ_SIZE; i = i + 1) begin
                ez_score[i] <= {SCORE_W{1'b0}};
                ez_id[i]    <= {ID_W{1'b0}};
            end
            for (i = 0; i < SZ_SIZE; i = i + 1) begin
                sz_score[i] <= {SCORE_W{1'b0}};
                sz_id[i]    <= {ID_W{1'b0}};
            end
        end else begin
            case (heap_state)
                //=============================================================
                // HEAPIFY: build a valid min-heap bottom-up
                //=============================================================
                ST_HFY_NODE2: begin
                    if (hfy_node2_swap) begin
                        ez_score[2] <= hfy_node2_minv;
                        ez_score[hfy_node2_minc] <= hfy_node2_val;
                        ez_id[2]    <= ez_id[hfy_node2_minc];
                        ez_id[hfy_node2_minc] <= ez_id[2];
                    end
                    heap_state <= ST_HFY_NODE1;
                    admitted_valid <= 1'b0;
                end
                ST_HFY_NODE1: begin
                    if (hfy_node1_swap) begin
                        ez_score[1] <= hfy_node1_minv;
                        ez_score[hfy_node1_minc] <= hfy_node1_val;
                        ez_id[1]    <= ez_id[hfy_node1_minc];
                        ez_id[hfy_node1_minc] <= ez_id[1];
                    end
                    heap_state <= ST_HFY_NODE0;
                    admitted_valid <= 1'b0;
                end
                ST_HFY_NODE0: begin
                    if (hfy_node0_swap) begin
                        ez_score[0] <= hfy_node0_minv;
                        ez_score[hfy_node0_minc] <= hfy_node0_val;
                        ez_id[0]    <= ez_id[hfy_node0_minc];
                        ez_id[hfy_node0_minc] <= ez_id[0];
                        // Register the demoted value and child index for
                        // the continuation cycle (avoids combinational race).
                        hfy_cont_val_r <= hfy_node0_val;
                        hfy_cont_idx_r <= hfy_node0_minc[2:0];
                        heap_state <= ST_HFY_NODE0_CONT;
                    end else begin
                        safe_min     <= sz_min_comb;
                        safe_min_idx <= sz_min_idx_comb;
                        heap_state   <= ST_ADMIT;
                    end
                    admitted_valid <= 1'b0;
                end
                ST_HFY_NODE0_CONT: begin
                    if (hfy_cont_swap) begin
                        ez_score[hfy_cont_idx_r] <= hfy_cont_minv;
                        ez_score[hfy_cont_minc]  <= hfy_cont_val_r;
                        ez_id[hfy_cont_idx_r]    <= ez_id[hfy_cont_minc];
                        ez_id[hfy_cont_minc]     <= ez_id[hfy_cont_idx_r];
                    end
                    safe_min     <= sz_min_comb;
                    safe_min_idx <= sz_min_idx_comb;
                    heap_state   <= ST_ADMIT;
                    admitted_valid <= 1'b0;
                end

                //=============================================================
                // FILL: accumulate first K descriptors
                //=============================================================
                ST_FILL: begin
                    if (s5a_valid && s5a_case == 2'b11) begin
                        admitted_valid  <= 1'b1;
                        admitted        <= 1'b1;
                        if (count < EZ_SIZE[4:0]) begin
                            ez_score[count[2:0]] <= s5a_score;
                            ez_id[count[2:0]]    <= s5a_id;
                        end else begin
                            sz_score[count - EZ_SIZE[4:0]] <= s5a_score;
                            sz_id[count - EZ_SIZE[4:0]]    <= s5a_id;
                        end
                        count <= count + 5'd1;
                        if (count >= EZ_SIZE[4:0]) begin
                            safe_min     <= sz_min_comb;
                            safe_min_idx <= sz_min_idx_comb;
                        end
                        if (count + 5'd1 == K[4:0]) begin
                            heap_state <= ST_HFY_NODE2;
                        end
                    end else begin
                        admitted_valid <= 1'b0;
                    end
                end

                //=============================================================
                // ADMIT: three-branch replacement
                //=============================================================
                ST_ADMIT: begin
                    if (s5a_valid && s5a_case == 2'b01) begin
                        // Case 1: EZ-local replace
                        admitted_valid  <= 1'b1;
                        admitted        <= 1'b1;
                        if (l0_go_left) begin
                            ez_score[0] <= ez_score[1]; ez_id[0] <= ez_id[1];
                            if (l1_go_left_via1) begin
                                ez_score[1] <= ez_score[3]; ez_id[1] <= ez_id[3];
                                ez_score[3] <= s5a_score;   ez_id[3] <= s5a_id;
                            end else if (l1_go_right_via1) begin
                                ez_score[1] <= ez_score[4]; ez_id[1] <= ez_id[4];
                                ez_score[4] <= s5a_score;   ez_id[4] <= s5a_id;
                            end else begin
                                ez_score[1] <= s5a_score;   ez_id[1] <= s5a_id;
                            end
                        end else if (l0_go_right) begin
                            ez_score[0] <= ez_score[2]; ez_id[0] <= ez_id[2];
                            if (l1_go_left_via2) begin
                                ez_score[2] <= ez_score[5]; ez_id[2] <= ez_id[5];
                                ez_score[5] <= s5a_score;   ez_id[5] <= s5a_id;
                            end else if (l1_go_right_via2) begin
                                ez_score[2] <= ez_score[6]; ez_id[2] <= ez_id[6];
                                ez_score[6] <= s5a_score;   ez_id[6] <= s5a_id;
                            end else begin
                                ez_score[2] <= s5a_score;   ez_id[2] <= s5a_id;
                            end
                        end else begin
                            ez_score[0] <= s5a_score; ez_id[0] <= s5a_id;
                        end
                    end else if (s5a_valid && s5a_case == 2'b10) begin
                        // Case 2: Cross-zone replace
                        admitted_valid  <= 1'b1;
                        admitted        <= 1'b1;

                        sz_score[safe_min_idx] <= s5a_score;
                        sz_id[safe_min_idx]    <= s5a_id;

                        if (l0_go_left) begin
                            ez_score[0] <= ez_score[1]; ez_id[0] <= ez_id[1];
                            if (l1_go_left_via1) begin
                                ez_score[1] <= ez_score[3]; ez_id[1] <= ez_id[3];
                                ez_score[3] <= safe_min;    ez_id[3] <= sz_id[safe_min_idx];
                            end else if (l1_go_right_via1) begin
                                ez_score[1] <= ez_score[4]; ez_id[1] <= ez_id[4];
                                ez_score[4] <= safe_min;    ez_id[4] <= sz_id[safe_min_idx];
                            end else begin
                                ez_score[1] <= safe_min;    ez_id[1] <= sz_id[safe_min_idx];
                            end
                        end else if (l0_go_right) begin
                            ez_score[0] <= ez_score[2]; ez_id[0] <= ez_id[2];
                            if (l1_go_left_via2) begin
                                ez_score[2] <= ez_score[5]; ez_id[2] <= ez_id[5];
                                ez_score[5] <= safe_min;    ez_id[5] <= sz_id[safe_min_idx];
                            end else if (l1_go_right_via2) begin
                                ez_score[2] <= ez_score[6]; ez_id[2] <= ez_id[6];
                                ez_score[6] <= safe_min;    ez_id[6] <= sz_id[safe_min_idx];
                            end else begin
                                ez_score[2] <= safe_min;    ez_id[2] <= sz_id[safe_min_idx];
                            end
                        end else begin
                            ez_score[0] <= safe_min; ez_id[0] <= sz_id[safe_min_idx];
                        end

                        safe_min     <= sz_min_fwd;
                        safe_min_idx <= sz_min_idx_fwd;
                    end else if (s5a_valid && s5a_case == 2'b00) begin
                        // Case 0: Reject
                        admitted_valid  <= 1'b1;
                        admitted        <= 1'b0;
                    end else begin
                        admitted_valid <= 1'b0;
                        // Refresh safe_min on idle cycles
                        safe_min     <= sz_min_comb;
                        safe_min_idx <= sz_min_idx_comb;
                    end
                end
                default: heap_state <= ST_FILL;
            endcase
        end
    end

    //=========================================================================
    // Readout FSM
    //=========================================================================
    reg [4:0] rd_idx;
    reg       rd_active;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n || flush) begin
            rd_active        <= 1'b0;
            rd_idx           <= 5'd0;
            readout_valid    <= 1'b0;
            readout_done     <= 1'b0;
            readout_chunk_id <= {ID_W{1'b0}};
            readout_score    <= {SCORE_W{1'b0}};
        end else if (readout_start && !rd_active) begin
            rd_active    <= 1'b1;
            rd_idx       <= 5'd0;
            readout_done <= 1'b0;
        end else if (rd_active) begin
            if (rd_idx < count) begin
                readout_valid <= 1'b1;
                if (rd_idx < EZ_SIZE[4:0]) begin
                    readout_chunk_id <= ez_id[rd_idx[2:0]];
                    readout_score    <= ez_score[rd_idx[2:0]];
                end else begin
                    readout_chunk_id <= sz_id[rd_idx - EZ_SIZE[4:0]];
                    readout_score    <= sz_score[rd_idx - EZ_SIZE[4:0]];
                end
                rd_idx <= rd_idx + 5'd1;
            end else begin
                readout_valid <= 1'b0;
                readout_done  <= 1'b1;
                rd_active     <= 1'b0;
            end
        end else begin
            readout_valid <= 1'b0;
        end
    end

endmodule
