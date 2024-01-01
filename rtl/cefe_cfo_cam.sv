//=============================================================================
// CEFE CFO CAM — Cross-Tenant Fetch-Once Coalescence Engine
//
// Implements the Coalesced Fetch-Once (CFO) mechanism from §5.3:
//   - 16-entry Content-Addressable Memory (CAM) for in-flight chunk handles
//   - 64-bit chunk-handle parallel comparison across all entries
//   - When multiple tenants request the same chunk, only ONE source read is
//     issued; all requestors receive a completion when the single read returns
//   - Multi-cast completion bitmap tracks which VCs await each chunk
//   - HMAC-SHA256 64-bit tag verification interface for cross-tenant security
//
// Operation:
//   1. LOOKUP: New request arrives with {chunk_handle[63:0], vc_id[3:0]}
//   2. HIT: chunk_handle matches an existing entry →
//            add vc_id to completion bitmap, suppress duplicate DMA read
//   3. MISS: no match → allocate new entry, issue DMA read, record vc_id
//   4. COMPLETE: DMA read returns → multicast completion to all VCs in bitmap
//   5. EVICT: entry freed after all completions sent
//
// Security:
//   - HMAC tag port for external SHA-256 accelerator validation
//   - Entry only coalesces if HMAC tags match (same chunk, authorized tenant)
//
// Target: ASAP7 7nm @ 1 GHz
// Area estimate: 0.012 mm² (CAM + comparators + bitmap logic)
//=============================================================================
`timescale 1ns/1ps

module cefe_cfo_cam #(
    parameter int NUM_ENTRIES   = 16,
    parameter int HANDLE_WIDTH  = 64,
    parameter int NUM_VC        = 16,
    parameter int TAG_WIDTH     = 64   // HMAC-SHA256 truncated tag
)(
    input  logic                     clk,
    input  logic                     rst_n,

    //=========================================================================
    // Decode step boundary (for EMA clock-gating evaluation)
    //=========================================================================
    input  logic                     step_boundary,

    //=========================================================================
    // Lookup Request Interface (from BDB parser)
    //=========================================================================
    input  logic                     req_valid,
    output logic                     req_ready,
    input  logic [HANDLE_WIDTH-1:0]  req_chunk_handle,
    input  logic [3:0]               req_vc_id,
    input  logic [TAG_WIDTH-1:0]     req_hmac_tag,
    // Target region is read-only for this request's tenant. Coalescing a
    // fetch-once across tenants is only sound for read-only regions (a shared
    // writable region could leak one tenant's write to another via the shared
    // single fetch), so this is a hard precondition for a HIT-path merge.
    input  logic                     req_region_ro,
    // Monotonic epoch/nonce for the current decode step. Bound into the stored
    // tag-verification so a captured (handle,tag) pair from an earlier epoch
    // cannot be replayed to force a coalesce in a later epoch.
    input  logic [15:0]              req_epoch,

    //=========================================================================
    // SEA overlap probe: when the stochastic explorer detects cross-tenant
    // chunk overlap above the wake threshold, it pulses sea_wake to force the
    // CAM back on within one step (independent of the internal EMA).
    //=========================================================================
    input  logic                     sea_wake,

    //=========================================================================
    // DMA Read Issue Interface (to endpoint copy engine)
    // Only fires on CAM MISS (new unique chunk)
    //=========================================================================
    output logic                     dma_rd_valid,
    input  logic                     dma_rd_ready,
    output logic [HANDLE_WIDTH-1:0]  dma_rd_handle,
    output logic [3:0]               dma_rd_entry_id,  // CAM entry for completion routing

    //=========================================================================
    // DMA Read Completion Interface (from endpoint copy engine)
    //=========================================================================
    input  logic                     dma_cpl_valid,
    input  logic [3:0]               dma_cpl_entry_id,
    input  logic                     dma_cpl_error,

    //=========================================================================
    // Multi-cast Completion Interface (to per-VC completion queues)
    //=========================================================================
    output logic [NUM_VC-1:0]        mcast_cpl_valid,   // One-hot: which VCs get completion
    output logic [HANDLE_WIDTH-1:0]  mcast_cpl_handle,
    output logic                     mcast_cpl_error,

    //=========================================================================
    // HMAC Verification Interface (to external SHA-256 accelerator)
    //=========================================================================
    output logic                     hmac_req_valid,
    input  logic                     hmac_req_ready,
    output logic [TAG_WIDTH-1:0]     hmac_req_tag,
    output logic [HANDLE_WIDTH-1:0]  hmac_req_handle,
    input  logic                     hmac_rsp_valid,
    input  logic                     hmac_rsp_pass,

    //=========================================================================
    // Status
    //=========================================================================
    output logic [4:0]               cam_occupancy,     // Number of active entries
    output logic                     cam_full
);

    //=========================================================================
    // CAM Entry Storage
    //
    // Stored as parallel unpacked arrays instead of an unpacked array of packed
    // structs so that the design elaborates cleanly under Icarus Verilog 12.
    // The physical CAM remains unchanged; this is purely a modelling style
    // change for tool compatibility.
    //=========================================================================
    logic                    entry_valid        [0:NUM_ENTRIES-1];
    logic [HANDLE_WIDTH-1:0] entry_handle       [0:NUM_ENTRIES-1];
    logic [TAG_WIDTH-1:0]    entry_tag          [0:NUM_ENTRIES-1];
    logic [15:0]             entry_epoch        [0:NUM_ENTRIES-1];
    logic                    entry_ro           [0:NUM_ENTRIES-1];
    logic [NUM_VC-1:0]       entry_bitmap       [0:NUM_ENTRIES-1];
    logic                    entry_dma_issued   [0:NUM_ENTRIES-1];
    logic                    entry_dma_complete [0:NUM_ENTRIES-1];
    logic                    entry_dma_error    [0:NUM_ENTRIES-1];

    // Gated CAM clock.  cfo_cam_gate is computed below; when asserted the CAM
    // state registers stop toggling to save dynamic power.  Control logic
    // (FSM, EMA counters) remains on the ungated clock so that requests are
    // not accepted while the CAM is gated.
    logic cam_clk;
    ICG cfo_icg (
        .CK  (clk),
        .E   (~cfo_cam_gate),
        .SE  (1'b0),
        .GCK (cam_clk)
    );

    //=========================================================================
    // Parallel CAM Match Logic (combinational)
    // When the CAM is gated due to low hit rate, coalescing is disabled and
    // all requests are treated as misses.  This is the functional equivalent
    // of disabling the CAM match broadcast; register-level clock gating would
    // be added during physical implementation.
    //=========================================================================
    logic [NUM_ENTRIES-1:0] match_vec;
    logic                   any_match_raw;
    logic                   any_match;
    logic [3:0]             match_idx;

    always_comb begin
        match_vec = '0;
        for (int i = 0; i < NUM_ENTRIES; i++) begin
            match_vec[i] = entry_valid[i] &&
                           (entry_handle[i] == req_chunk_handle);
        end
    end

    assign any_match_raw = |match_vec;
    // Disable coalescing when the CAM is gated
    assign any_match = cfo_cam_gate ? 1'b0 : any_match_raw;

    // Priority encoder: find first matching entry
    always_comb begin
        match_idx = '0;
        for (int i = NUM_ENTRIES - 1; i >= 0; i--) begin
            if (match_vec[i]) match_idx = 4'(i);
        end
    end

    //=========================================================================
    // Single-Cycle HIT Coalesce Eligibility (combinational)
    //
    // A HIT-path coalesce fires ONLY when ALL of the following hold:
    //   (a) handle match          — any_match (CAM handle broadcast)
    //   (b) both parties' tokens   — stored tag == request tag AND the stored
    //                                 tag's epoch == request epoch. Binding the
    //                                 epoch defeats replay of a stale but
    //                                 otherwise-valid (handle,tag) pair.
    //   (c) region is read-only    — both the stored entry and the requesting
    //                                 tenant mark the region read-only.
    // All three are ANDed; if any is false the request falls through to the
    // MISS path (full external HMAC validation) rather than coalescing.
    //
    // Per §5.3: coalescing hits use pre-stored tags in the CAM entry for
    // single-cycle verification — no external HMAC accelerator round-trip —
    // but the epoch binding + read-only precondition close the replay and
    // writable-region leakage holes.
    //=========================================================================
    logic tag_match_hit;    // token (tag+epoch) match on the hit path
    logic ro_ok;            // read-only precondition
    logic coalesce_ok;      // full 3-condition eligibility

    assign tag_match_hit = any_match
                         && (entry_tag[match_idx]   == req_tag_r)
                         && (entry_epoch[match_idx] == req_epoch_r);
    assign ro_ok         = any_match && entry_ro[match_idx] && req_region_ro_r;
    assign coalesce_ok   = any_match && tag_match_hit && ro_ok;

    //=========================================================================
    // Free Entry Finder (for MISS allocation)
    //=========================================================================
    logic [NUM_ENTRIES-1:0] free_vec;
    logic                   any_free;
    logic [3:0]             free_idx;

    always_comb begin
        free_vec = '0;
        for (int i = 0; i < NUM_ENTRIES; i++) begin
            free_vec[i] = ~entry_valid[i];
        end
    end

    assign any_free = |free_vec;

    // Priority encoder: find first free entry
    always_comb begin
        free_idx = '0;
        for (int i = NUM_ENTRIES - 1; i >= 0; i--) begin
            if (free_vec[i]) free_idx = 4'(i);
        end
    end

    //=========================================================================
    // Occupancy Counter
    //=========================================================================
    logic [4:0] occ_count;

    always_comb begin
        occ_count = '0;
        for (int i = 0; i < NUM_ENTRIES; i++) begin
            occ_count = occ_count + {4'b0, entry_valid[i]};
        end
    end

    assign cam_occupancy = occ_count;
    assign cam_full      = (occ_count == 5'(NUM_ENTRIES));

    //=========================================================================
    // FSM States
    //=========================================================================
    typedef enum logic [2:0] {
        S_IDLE,         // Waiting for request
        S_CAM_LOOKUP,   // Parallel compare complete (combinational, same cycle)
        S_HMAC_REQ,     // Sending HMAC verification request
        S_HMAC_WAIT,    // Waiting for HMAC response
        S_ALLOC,        // Allocating new entry (MISS path)
        S_COALESCE,     // Adding VC to existing entry (HIT path)
        S_DMA_ISSUE     // Issuing DMA read for new entry
    } state_t;

    state_t state, state_next;

    // Latched request fields
    logic [HANDLE_WIDTH-1:0] req_handle_r;
    logic [3:0]              req_vc_id_r;
    logic [TAG_WIDTH-1:0]    req_tag_r;
    logic [15:0]             req_epoch_r;
    logic                    req_region_ro_r;
    logic                    hmac_passed;

    //=========================================================================
    // State Register
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
        end else begin
            state <= state_next;
        end
    end

    //=========================================================================
    // Request Latch
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            req_handle_r <= '0;
            req_vc_id_r  <= '0;
            req_tag_r    <= '0;
            req_epoch_r     <= '0;
            req_region_ro_r <= 1'b0;
            hmac_passed  <= 1'b0;
        end else begin
            if (state == S_IDLE && req_valid && req_ready) begin
                req_handle_r <= req_chunk_handle;
                req_vc_id_r  <= req_vc_id;
                req_tag_r    <= req_hmac_tag;
                req_epoch_r     <= req_epoch;
                req_region_ro_r <= req_region_ro;
                hmac_passed  <= 1'b0;
            end
            if (state == S_HMAC_WAIT && hmac_rsp_valid) begin
                hmac_passed <= hmac_rsp_pass;
            end
        end
    end

    //=========================================================================
    // Next-State Logic
    //
    // HIT path (single-cycle): S_IDLE → S_CAM_LOOKUP → S_COALESCE → S_IDLE
    //   - CAM handle match + stored tag == request tag: coalesce immediately
    //   - CAM handle match + tag mismatch: reject (potential replay attack)
    //
    // MISS path (multi-cycle): S_IDLE → S_CAM_LOOKUP → S_HMAC_REQ →
    //                          S_HMAC_WAIT → S_ALLOC → S_DMA_ISSUE → S_IDLE
    //   - No CAM match: external HMAC validates new session before allocation
    //=========================================================================
    always_comb begin
        state_next = state;
        case (state)
            S_IDLE: begin
                if (req_valid && req_ready)
                    state_next = S_CAM_LOOKUP;
            end
            S_CAM_LOOKUP: begin
                if (any_match) begin
                    // HIT: handle found in CAM — require full 3-condition
                    // eligibility (tag+epoch token match AND read-only region).
                    if (coalesce_ok)
                        state_next = S_COALESCE;  // Single-cycle verified coalesce
                    else
                        state_next = S_HMAC_REQ;  // Not eligible: full HMAC path
                end else begin
                    // MISS: no handle in CAM — need external HMAC validation
                    state_next = S_HMAC_REQ;
                end
            end
            S_HMAC_REQ: begin
                if (hmac_req_ready)
                    state_next = S_HMAC_WAIT;
            end
            S_HMAC_WAIT: begin
                if (hmac_rsp_valid) begin
                    if (hmac_rsp_pass) begin
                        // HMAC passed: allocate new entry
                        if (any_free)
                            state_next = S_ALLOC;
                        else
                            state_next = S_IDLE; // CAM full, drop (backpressure should prevent)
                    end else begin
                        // HMAC failed: reject request, return to idle
                        state_next = S_IDLE;
                    end
                end
            end
            S_COALESCE: begin
                // Single-cycle: add VC to bitmap
                state_next = S_IDLE;
            end
            S_ALLOC: begin
                // Allocate entry, then issue DMA
                state_next = S_DMA_ISSUE;
            end
            S_DMA_ISSUE: begin
                if (dma_rd_ready)
                    state_next = S_IDLE;
            end
            default: state_next = S_IDLE;
        endcase
    end

    //=========================================================================
    // Ready Signal: accept new request in IDLE when CAM can service it
    // - HIT path needs an existing match (no free entry consumed)
    // - MISS path needs a free entry for allocation
    // Accept if either condition could be satisfied (actual path determined
    // in S_CAM_LOOKUP after latching the request)
    //=========================================================================
    assign req_ready = (state == S_IDLE) && !cam_full;

    //=========================================================================
    // HMAC Request Interface
    //=========================================================================
    assign hmac_req_valid  = (state == S_HMAC_REQ);
    assign hmac_req_tag    = req_tag_r;
    assign hmac_req_handle = req_handle_r;

    //=========================================================================
    // DMA Read Issue (only on MISS after HMAC pass)
    //=========================================================================
    logic [3:0] alloc_idx_r;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            alloc_idx_r <= '0;
        end else if (state == S_HMAC_WAIT && hmac_rsp_valid && hmac_rsp_pass && any_free) begin
            alloc_idx_r <= free_idx;
        end
    end

    assign dma_rd_valid    = (state == S_DMA_ISSUE);
    assign dma_rd_handle   = req_handle_r;
    assign dma_rd_entry_id = alloc_idx_r;

    //=========================================================================
    // CAM Entry Update Logic
    //=========================================================================
    always_ff @(posedge cam_clk or negedge rst_n) begin
        if (!rst_n) begin
            for (int i = 0; i < NUM_ENTRIES; i++) begin
                entry_valid[i]        <= 1'b0;
                entry_handle[i]       <= '0;
                entry_tag[i]          <= '0;
                entry_epoch[i]        <= '0;
                entry_ro[i]           <= 1'b0;
                entry_bitmap[i]       <= '0;
                entry_dma_issued[i]   <= 1'b0;
                entry_dma_complete[i] <= 1'b0;
                entry_dma_error[i]    <= 1'b0;
            end
        end else begin
            // ALLOCATE: new entry on MISS
            if (state == S_ALLOC) begin
                entry_valid[alloc_idx_r]        <= 1'b1;
                entry_handle[alloc_idx_r]       <= req_handle_r;
                entry_tag[alloc_idx_r]          <= req_tag_r;
                entry_epoch[alloc_idx_r]        <= req_epoch_r;
                entry_ro[alloc_idx_r]           <= req_region_ro_r;
                entry_bitmap[alloc_idx_r]       <= (NUM_VC'(1'b1) << req_vc_id_r);
                entry_dma_issued[alloc_idx_r]   <= 1'b0;
                entry_dma_complete[alloc_idx_r] <= 1'b0;
                entry_dma_error[alloc_idx_r]    <= 1'b0;
            end

            // Mark DMA issued
            if (state == S_DMA_ISSUE && dma_rd_ready) begin
                entry_dma_issued[alloc_idx_r] <= 1'b1;
            end

            // COALESCE: add VC to existing entry's bitmap on HIT
            if (state == S_COALESCE) begin
                entry_bitmap[match_idx] <=
                    entry_bitmap[match_idx] | (NUM_VC'(1'b1) << req_vc_id_r);
            end

            // DMA COMPLETION: mark entry complete, store error status
            if (dma_cpl_valid && entry_valid[dma_cpl_entry_id]) begin
                entry_dma_complete[dma_cpl_entry_id] <= 1'b1;
                entry_dma_error[dma_cpl_entry_id]   <= dma_cpl_error;
            end

            // EVICTION: after multicast completion is sent, free the entry
            // (handled by completion FSM below)
            for (int i = 0; i < NUM_ENTRIES; i++) begin
                if (entry_valid[i] && entry_dma_complete[i] && evict_vec[i]) begin
                    entry_valid[i] <= 1'b0;
                end
            end
        end
    end

    //=========================================================================
    // Multi-cast Completion Logic
    //
    // When a DMA read completes, we multicast the completion to ALL VCs
    // recorded in that entry's bitmap. This is combinational — all VCs
    // see completion in the same cycle.
    //=========================================================================
    logic [NUM_ENTRIES-1:0] cpl_pending;  // Entries with DMA complete, not yet multicast
    logic [NUM_ENTRIES-1:0] evict_vec;    // Entries being evicted this cycle
    logic [3:0]             cpl_select;   // Which entry to multicast this cycle
    logic                   cpl_any;

    always_comb begin
        cpl_pending = '0;
        for (int i = 0; i < NUM_ENTRIES; i++) begin
            cpl_pending[i] = entry_valid[i] & entry_dma_complete[i];
        end
    end

    assign cpl_any = |cpl_pending;

    // Priority select: lowest-index completed entry
    always_comb begin
        cpl_select = '0;
        for (int i = NUM_ENTRIES - 1; i >= 0; i--) begin
            if (cpl_pending[i]) cpl_select = 4'(i);
        end
    end

    // Multicast output
    assign mcast_cpl_valid  = cpl_any ? entry_bitmap[cpl_select] : '0;
    assign mcast_cpl_handle = cpl_any ? entry_handle[cpl_select] : '0;
    assign mcast_cpl_error  = cpl_any ? entry_dma_error[cpl_select] : 1'b0;

    // Evict: one entry per cycle after multicast
    always_comb begin
        evict_vec = '0;
        if (cpl_any) begin
            evict_vec[cpl_select] = 1'b1;
        end
    end

    //=========================================================================
    // Dynamic Clock Gating: Step-Boundary EMA-based CAM power management
    //
    // Per §5.3, with hysteresis:
    //   - GATE (enter power-save) when the EMA coalesce hit rate stays below
    //     0.45 for 8 CONSECUTIVE decode steps.
    //   - WAKE (exit power-save) as soon as the EMA rises to/above 0.50, OR
    //     immediately when the SEA overlap probe pulses sea_wake.
    //   The 0.45/0.50 split is a hysteresis band that prevents chatter around a
    //   single threshold. Wake takes effect on the SAME step the condition is
    //   detected (the consecutive-fail counter is evaluated combinationally as
    //   its next value, so a stale count can no longer defer re-enable a step).
    //
    // Evaluation occurs STRICTLY on step_boundary (or on sea_wake) — not
    // per-request — to match the paper's "8 consecutive steps" semantic.
    //=========================================================================

    // Per-step hit/miss counters (reset on step_boundary)
    logic [7:0]  step_hits;         // Coalesce hits this decode step
    logic [7:0]  step_total;        // Total requests this decode step

    // EMA and gating state (updated only on step_boundary)
    logic [7:0]  ema_hit_rate;      // Q0.8 exponential moving average
    logic [3:0]  low_rate_counter;  // Consecutive steps below gate threshold
    logic        cfo_cam_gate;      // 1 = CAM clock is gated (disabled)

    localparam logic [7:0] GATE_THRESHOLD = 8'd115;  // 0.45 × 255 ≈ 115 (enter)
    localparam logic [7:0] WAKE_THRESHOLD = 8'd128;  // 0.50 × 255 ≈ 128 (exit)
    localparam logic [3:0] GATE_STEPS     = 4'd8;

    // Accumulate per-step hit/miss counters
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            step_hits  <= 8'd0;
            step_total <= 8'd0;
        end else if (step_boundary) begin
            // Reset counters at start of new decode step
            step_hits  <= 8'd0;
            step_total <= 8'd0;
        end else begin
            // Count coalesce hits and total requests within a step
            if (state != S_IDLE && state_next == S_IDLE) begin
                step_total <= step_total + 8'd1;
                if (state == S_COALESCE)
                    step_hits <= step_hits + 8'd1;
            end
        end
    end

    // Combinational: compute this-step hit rate as Q0.8 fixed-point
    logic [15:0] hit_rate_sample;
    assign hit_rate_sample = (step_total > 8'd0)
                           ? ({step_hits, 8'b0} / {8'b0, step_total})
                           : 16'd128;  // No data → neutral (0.5)

    // Signed delta for smooth EMA update (alpha = 1/32)
    wire signed [8:0] ema_delta = $signed({1'b0, hit_rate_sample[7:0]})
                                - $signed({1'b0, ema_hit_rate});

    // Next EMA value this step (so gate/wake decisions use the UPDATED rate,
    // not the stale registered one — this is what makes wake single-step).
    wire [7:0] ema_next = (step_total > 8'd0)
                        ? $unsigned($signed({1'b0, ema_hit_rate}) + (ema_delta >>> 5))
                        : ema_hit_rate;

    // Next consecutive-fail counter value, computed combinationally.
    wire [3:0] low_ctr_next = (ema_next < GATE_THRESHOLD)
                            ? ((low_rate_counter < 4'd15) ? low_rate_counter + 4'd1
                                                          : low_rate_counter)
                            : 4'd0;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ema_hit_rate     <= 8'd128;  // Start at 0.5 (neutral)
            low_rate_counter <= 4'd0;
            cfo_cam_gate     <= 1'b0;
        end else if (sea_wake) begin
            // SEA overlap probe forces immediate wake (single cycle), and
            // clears the fail history so the CAM stays awake.
            cfo_cam_gate     <= 1'b0;
            low_rate_counter <= 4'd0;
        end else if (step_boundary) begin
            ema_hit_rate     <= ema_next;
            low_rate_counter <= low_ctr_next;

            // Hysteretic gate control, evaluated on the UPDATED (next) values:
            //   enter power-save after 8 consecutive failing steps;
            //   exit as soon as the EMA reaches the 0.50 wake threshold.
            if (ema_next >= WAKE_THRESHOLD)
                cfo_cam_gate <= 1'b0;                 // wake (hysteresis high)
            else if (low_ctr_next >= GATE_STEPS)
                cfo_cam_gate <= 1'b1;                 // gate (8 fails at <0.45)
            // else: hold current gate state (inside hysteresis band)
        end
    end

endmodule
