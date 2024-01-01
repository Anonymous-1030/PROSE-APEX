//=============================================================================
// CEFE Per-Transfer Pin Table  (paper §III-B, Invariant 1 / Theorem 1)
//
// Implements the transfer-lifetime binding hold that the Object Admission
// Transaction (OAT) acquires atomically with generation validation:
//
//   Invariant 1:  ISSUE(d) <= t < COMPLETE(d)  =>  MAP[id]=<slot,g>
//                                                   /\ PIN(id,g) > 0
//                 and PIN(id,g) > 0 forbids reassigning/overwriting slot.
//
// The table holds at most one in-flight batch per tenant. With 16 tenants and
// up to 25 admits per batch, the bound is 400 entries (paper §III-B: "400 slots
// within the SRAM budget"). Each entry pins one (tenant, chunk, generation).
//
// Three ports, all single-cycle:
//   * ALLOCATE : on an OAT pass, atomically install a pin. Combinational
//                alloc_ok tells the caller whether a free entry existed; the
//                write commits on the same clock edge (one linearization point).
//   * RELEASE  : on completion or abort, RELEASE(d) decrements/frees the pin.
//   * RECLAIM  : the object directory probes before evict/reuse; reclaim_allowed
//                is combinational and true iff NO entry pins (chunk,generation).
//                Reclaim/overwrite is therefore legal only at zero pin count.
//
// This is the hardware realization of the pin discipline the TLA+ model
// (formal/prose_oat.tla) and the Python checker (formal/check_oat_model.py)
// prove sufficient for zero stale payload.
//
// Target: ASAP7 7nm @ 1 GHz. 400 x {valid, tenant[3:0], chunk[8:0], gen[15:0]}
// = 400 x 30 b ~= 1.5 KiB, within the 216 KiB on-chip state budget.
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module cefe_pin_table #(
    parameter int NUM_ENTRIES = 400,
    parameter int TENANT_W    = 4,
    parameter int CHUNK_W     = 9,
    parameter int GEN_W       = 16
) (
    input  logic                 clk,
    input  logic                 rst_n,

    // --- ALLOCATE port (from OAT pass at S2) ---
    input  logic                 alloc_valid,
    input  logic [TENANT_W-1:0]  alloc_tenant,
    input  logic [CHUNK_W-1:0]   alloc_chunk,
    input  logic [GEN_W-1:0]     alloc_gen,
    output logic                 alloc_ok,       // comb: a free entry exists
    output logic [$clog2(NUM_ENTRIES)-1:0] alloc_index,

    // --- RELEASE port (from completion / abort at S8) ---
    input  logic                 release_valid,
    input  logic [CHUNK_W-1:0]   release_chunk,
    input  logic [GEN_W-1:0]     release_gen,

    // --- RECLAIM probe port (from object directory / fabric-manager) ---
    input  logic [CHUNK_W-1:0]   reclaim_chunk,
    input  logic [GEN_W-1:0]     reclaim_gen,
    output logic                 reclaim_allowed, // comb: no pin protects (chunk,gen)

    // --- Status ---
    output logic [$clog2(NUM_ENTRIES+1)-1:0] pin_count,
    output logic                 table_full
);

    localparam int IDX_W = $clog2(NUM_ENTRIES);

    // Entry storage
    logic                 v      [0:NUM_ENTRIES-1];
    logic [TENANT_W-1:0]  tnt    [0:NUM_ENTRIES-1];
    logic [CHUNK_W-1:0]   chk    [0:NUM_ENTRIES-1];
    logic [GEN_W-1:0]     gen    [0:NUM_ENTRIES-1];

    //=========================================================================
    // Combinational lookups (priority encoders / match trees)
    //=========================================================================
    // First free entry (for allocation)
    logic                 free_found;
    logic [IDX_W-1:0]     free_idx;

    // Match for release: entry holding (release_chunk, release_gen)
    logic                 rel_found;
    logic [IDX_W-1:0]     rel_idx;

    // Match for reclaim: any entry pinning (reclaim_chunk, reclaim_gen)
    logic                 reclaim_match;

    integer i;
    always_comb begin
        free_found = 1'b0;
        free_idx   = '0;
        rel_found  = 1'b0;
        rel_idx    = '0;
        reclaim_match = 1'b0;
        for (i = 0; i < NUM_ENTRIES; i = i + 1) begin
            if (!v[i] && !free_found) begin
                free_found = 1'b1;
                free_idx   = i[IDX_W-1:0];
            end
            if (v[i] && chk[i] == release_chunk && gen[i] == release_gen
                     && !rel_found) begin
                rel_found = 1'b1;
                rel_idx   = i[IDX_W-1:0];
            end
            if (v[i] && chk[i] == reclaim_chunk && gen[i] == reclaim_gen) begin
                reclaim_match = 1'b1;
            end
        end
    end

    assign alloc_ok        = free_found;
    assign alloc_index     = free_idx;
    // Reclaim is legal ONLY when no pin protects the (chunk,generation) binding.
    assign reclaim_allowed = ~reclaim_match;

    //=========================================================================
    // State update — single linearization point.
    // ALLOCATE and RELEASE commit on the clock edge. An allocate to the same
    // free index as a same-cycle release cannot collide because release targets
    // a *matched valid* entry and allocate targets a *free* entry; the priority
    // encoders select disjoint indices by construction.
    //=========================================================================
    logic [$clog2(NUM_ENTRIES+1)-1:0] cnt_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < NUM_ENTRIES; i = i + 1) begin
                v[i] <= 1'b0;
            end
            cnt_q <= '0;
        end else begin
            // RELEASE first (frees an entry this cycle)
            if (release_valid && rel_found) begin
                v[rel_idx] <= 1'b0;
            end
            // ALLOCATE (installs a pin). If the table is full (no free entry),
            // alloc_ok is low and the OAT must reject — no pin is installed.
            if (alloc_valid && free_found) begin
                v[free_idx]   <= 1'b1;
                tnt[free_idx] <= alloc_tenant;
                chk[free_idx] <= alloc_chunk;
                gen[free_idx] <= alloc_gen;
            end
            // Maintain pin count (net change)
            case ({ (alloc_valid && free_found),
                    (release_valid && rel_found) })
                2'b10:   cnt_q <= cnt_q + 1'b1;   // alloc only
                2'b01:   cnt_q <= cnt_q - 1'b1;   // release only
                default: cnt_q <= cnt_q;          // both or neither
            endcase
        end
    end

    assign pin_count  = cnt_q;
    assign table_full = ~free_found;

endmodule
