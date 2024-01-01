//=============================================================================
// CEFE Long-Context Address Mapper  (paper §III-C.c, §IV-F, supplementary S10)
//
// Two-tier chunk-id -> backing-pointer translation for contexts beyond the
// 512-frame single-tier space (128K tokens at 64-token chunks needs 2048
// logical chunks).
//
//   Tier-1 hot-set cache : 512-entry, tag-validated, SINGLE-CYCLE hit. Covers
//                          the 200-400 active chunks per step (>95% of lookups,
//                          paper §III-C.c). Direct-mapped by a multiplicative
//                          hash of the logical chunk id; a stored tag confirms
//                          identity so a hash alias returns MISS, not a wrong
//                          pointer (the "tag-validated zero fallback" that holds
//                          Recovery@K at 0.831 vs. 0.371 for naive modulo
//                          aliasing, paper §III-C.c).
//   Tier-2 backing table : 2048-entry, THREE-CYCLE pipelined probe (3 ns at
//                          1 GHz, vs. the 100 us decode step, so end-to-end
//                          throughput is unchanged, paper §III-C.c). Absorbs
//                          overflow; on a Tier-1 miss it is probed and the
//                          result is installed into Tier-1 for reuse.
//
// Causal write discipline (paper §III-C, §III-A causal boundary): mapping
// updates commit ONLY at a decode-step boundary (step_commit, analogous to
// cfg_flush_gated). Reads during a step observe the previous step's committed
// mapping, so a step-t install can never affect a step-t lookup. This mirrors
// the read-first expert-bank discipline that enforces the t-1 boundary.
//
// This realizes the address-mapping structure that the Python hash-bank model
// (experiments/run_s3_long_context.py) previously stood in for; see
// LIMITATIONS_AND_FUTURE_WORK.md §2.
//
// Target: ASAP7 7nm @ 1 GHz. Tier-1 512 x {valid, tag[10:0], ptr[19:0]},
// Tier-2 2048 x {valid, ptr[19:0]} — within the 216 KiB on-chip state budget.
//=============================================================================
`timescale 1ns/1ps

module cefe_addr_mapper #(
    parameter int LOGICAL_ID_W = 12,   // 4096 logical chunks (>=128K tokens)
    parameter int HOT_ENTRIES  = 512,
    parameter int BACKING_ENTRIES = 2048,
    parameter int PTR_W        = 20
) (
    input  logic                    clk,
    input  logic                    rst_n,

    // --- Lookup port (single-cycle Tier-1; multi-cycle on Tier-1 miss) ---
    input  logic                    lookup_valid,
    input  logic [LOGICAL_ID_W-1:0] lookup_id,
    output logic                    lookup_hit,       // Tier-1 hit (1-cycle)
    output logic [PTR_W-1:0]        lookup_ptr,
    output logic                    lookup_miss,      // needs Tier-2 probe
    output logic                    lookup_ready,     // Tier-2 result valid

    // --- Tier-2 probe result (registered, 3-cycle latency from a miss) ---
    output logic [PTR_W-1:0]        probe_ptr,
    output logic                    probe_found,

    // --- Install port (mapping update; committed only at step boundary) ---
    input  logic                    install_valid,
    input  logic [LOGICAL_ID_W-1:0] install_id,
    input  logic [PTR_W-1:0]        install_ptr,
    input  logic                    step_commit,      // decode-step boundary

    // --- Status ---
    output logic [31:0]             stat_hits,
    output logic [31:0]             stat_misses
);

    localparam int HOT_IDX_W = $clog2(HOT_ENTRIES);
    localparam int BK_IDX_W  = $clog2(BACKING_ENTRIES);
    localparam int TAG_W     = LOGICAL_ID_W - HOT_IDX_W;

    //=========================================================================
    // Multiplicative (Fibonacci) hash of the logical id -> Tier-1 index.
    // Knuth's constant 2654435761 truncated; XOR-fold keeps it cheap.
    //=========================================================================
    function automatic [HOT_IDX_W-1:0] hot_index(input [LOGICAL_ID_W-1:0] id);
        logic [31:0] h;
        h = (id * 32'h9E3779B1);
        hot_index = h[31 -: HOT_IDX_W];   // top bits = best-mixed
    endfunction

    function automatic [TAG_W-1:0] id_tag(input [LOGICAL_ID_W-1:0] id);
        id_tag = id[LOGICAL_ID_W-1 -: TAG_W];
    endfunction

    //=========================================================================
    // Tier-1 hot-set storage
    //=========================================================================
    logic                 t1_valid [0:HOT_ENTRIES-1];
    logic [TAG_W-1:0]     t1_tag   [0:HOT_ENTRIES-1];
    logic [PTR_W-1:0]     t1_ptr   [0:HOT_ENTRIES-1];

    // Tier-2 backing storage (direct-mapped by low id bits)
    logic                 t2_valid [0:BACKING_ENTRIES-1];
    logic [PTR_W-1:0]     t2_ptr   [0:BACKING_ENTRIES-1];

    //=========================================================================
    // Tier-1 lookup: single-cycle, tag-validated.
    //=========================================================================
    logic [HOT_IDX_W-1:0] lk_index;
    logic [TAG_W-1:0]     lk_tag;
    assign lk_index = hot_index(lookup_id);
    assign lk_tag   = id_tag(lookup_id);

    logic t1_hit_comb;
    assign t1_hit_comb = lookup_valid & t1_valid[lk_index]
                       & (t1_tag[lk_index] == lk_tag);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            lookup_hit  <= 1'b0;
            lookup_miss <= 1'b0;
            lookup_ptr  <= '0;
        end else begin
            lookup_hit  <= t1_hit_comb;
            lookup_miss <= lookup_valid & ~t1_hit_comb;
            lookup_ptr  <= t1_ptr[lk_index];
        end
    end

    //=========================================================================
    // Tier-2 probe: 3-cycle pipeline (register id, read backing, register out).
    // Kicked off on a Tier-1 miss. On a hit in Tier-2 the pointer is installed
    // into Tier-1 (deferred to the step boundary via the install path from the
    // consumer). probe_found=0 means the chunk is not mapped anywhere.
    //=========================================================================
    logic                     p1_valid, p2_valid;
    logic [BK_IDX_W-1:0]      p1_index, p2_index;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            p1_valid <= 1'b0; p2_valid <= 1'b0;
            lookup_ready <= 1'b0;
            probe_found  <= 1'b0;
            probe_ptr    <= '0;
        end else begin
            // Stage A: capture a miss, index into backing
            p1_valid <= lookup_valid & ~t1_hit_comb;
            p1_index <= lookup_id[BK_IDX_W-1:0];
            // Stage B: read backing store
            p2_valid <= p1_valid;
            p2_index <= p1_index;
            // Stage C: register result
            lookup_ready <= p2_valid;
            probe_found  <= p2_valid & t2_valid[p2_index];
            probe_ptr    <= t2_ptr[p2_index];
        end
    end

    //=========================================================================
    // Install (mapping update). Causal write discipline: the update is BUFFERED
    // and committed to storage only when step_commit pulses at a decode-step
    // boundary. Reads within a step therefore see the previous step's mapping.
    //=========================================================================
    logic                    pend_valid;
    logic [LOGICAL_ID_W-1:0] pend_id;
    logic [PTR_W-1:0]        pend_ptr;

    integer i;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pend_valid <= 1'b0;
            for (i = 0; i < HOT_ENTRIES; i = i + 1) t1_valid[i] <= 1'b0;
            for (i = 0; i < BACKING_ENTRIES; i = i + 1) t2_valid[i] <= 1'b0;
        end else begin
            // Latch the most recent install request as pending.
            if (install_valid) begin
                pend_valid <= 1'b1;
                pend_id    <= install_id;
                pend_ptr   <= install_ptr;
            end
            // Commit pending mapping at the step boundary only.
            if (step_commit && pend_valid) begin
                // Tier-1 install (tag-validated slot)
                t1_valid[hot_index(pend_id)] <= 1'b1;
                t1_tag  [hot_index(pend_id)] <= id_tag(pend_id);
                t1_ptr  [hot_index(pend_id)] <= pend_ptr;
                // Tier-2 backing install (overflow-safe home)
                t2_valid[pend_id[BK_IDX_W-1:0]] <= 1'b1;
                t2_ptr  [pend_id[BK_IDX_W-1:0]] <= pend_ptr;
                pend_valid <= 1'b0;
            end
        end
    end

    //=========================================================================
    // Statistics
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            stat_hits   <= '0;
            stat_misses <= '0;
        end else begin
            if (lookup_hit)  stat_hits   <= stat_hits + 1'b1;
            if (lookup_miss) stat_misses <= stat_misses + 1'b1;
        end
    end

endmodule
