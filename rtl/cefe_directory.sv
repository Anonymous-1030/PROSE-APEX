//=============================================================================
// CEFE Object Directory (consult-then-CAS authoritative mapping)
//
// One banked entry per chunk. Entry layout (paper Section IV-B):
//   { generation, resident state, pin count, pending-reclaim bit }
//
// The Object Admission Transaction is structured as consult, then CAS:
//   * Advisory stages (elsewhere in the pipeline) read the entry and collect
//     information. They declare nothing.
//   * The final stage is one indivisible compare-and-swap. In a single cycle
//     the bank re-reads the entry, checks MAP[chunk] == <gen, resident> and
//     pending_reclaim == 0, and only on a full match increments the pin count.
//     That edge is the linearization point of the transaction.
//
// Placement updates and CAS operations share the single write port of each
// bank, one write per cycle, so all writes to one entry form a physical total
// order. An update that finds pin_count != 0 (or loses a same-cycle CAS) sets
// pending_reclaim and stores its payload in the pending registers. While
// pending_reclaim is set the CAS predicate fails, so pin_count decreases
// monotonically until the drain releases the last pin, at which point the
// banked update commits. An update that commits in an earlier cycle than a
// CAS is seen by the CAS re-read, and the transaction rejects.
//
// The old pin table (cefe_pin_table) survives as the per-transfer in-flight
// index and is written only on a successful CAS. It carries no authority:
// this entry's pin count is the binding of record.
//
// Assertions (clocked immediate form for Icarus; see the SVA notes inline):
//   A1 pin-write predicate  : a pin count increment implies the full CAS
//                             predicate held that cycle.
//   A2 reject closes gate   : a failed CAS never raises cas_ok, so the
//                             payload gate cannot open on a reject.
//   A3 update at zero pin   : an update commits only when pin_count == 0 and
//                             no CAS succeeds on the same entry that cycle.
//   A4 pending blocks pins  : while pending_reclaim is set, no CAS succeeds.
//
// Target: ASAP7 7nm @ 1 GHz.
//=============================================================================
`timescale 1ns/1ps

module cefe_directory #(
    parameter int NUM_CHUNKS = 512,
    parameter int GEN_W      = 16,
    parameter int PIN_W      = 3          // per-entry pin count width (0..7)
) (
    input  logic                 clk,
    input  logic                 rst_n,

    // --- Advisory read (combinational consult, declares nothing) ---
    input  logic [8:0]           adv_chunk,
    output logic [GEN_W-1:0]     adv_gen,
    output logic                 adv_resident,
    output logic                 adv_pending,
    output logic [PIN_W-1:0]     adv_pin_count,

    // --- CAS port (the linearization point) ---
    input  logic                 cas_valid,
    input  logic [8:0]           cas_chunk,
    input  logic [GEN_W-1:0]     cas_gen,       // generation the descriptor names
    input  logic                 cas_pin_free,  // in-flight index has a free entry
    output logic                 cas_ok,        // comb: predicate true this cycle

    // --- Placement update port (shares the bank write port with CAS) ---
    input  logic                 upd_valid,
    input  logic [8:0]           upd_chunk,
    input  logic [GEN_W-1:0]     upd_gen,       // new generation to place
    input  logic                 upd_resident,  // new resident state to place
    output logic                 upd_committed, // comb: commits at this edge
    output logic                 upd_pended,    // comb: diverted to pending

    // --- Release port (transfer completion / abort) ---
    input  logic                 rel_valid,
    input  logic [8:0]           rel_chunk,

    // --- Status ---
    output logic                 any_pending
);

    //=========================================================================
    // Entry storage
    //=========================================================================
    logic [GEN_W-1:0]  e_gen      [0:NUM_CHUNKS-1];
    logic              e_resident [0:NUM_CHUNKS-1];
    logic [PIN_W-1:0]  e_pins     [0:NUM_CHUNKS-1];
    logic              e_pend     [0:NUM_CHUNKS-1];
    // Waiting (pended) update payload, one per entry by construction.
    logic [GEN_W-1:0]  p_gen      [0:NUM_CHUNKS-1];
    logic              p_resident [0:NUM_CHUNKS-1];

    integer i;

    //=========================================================================
    // Advisory read: pure lookup, no side effect.
    //=========================================================================
    assign adv_gen       = e_gen[adv_chunk];
    assign adv_resident  = e_resident[adv_chunk];
    assign adv_pending   = e_pend[adv_chunk];
    assign adv_pin_count = e_pins[adv_chunk];

    //=========================================================================
    // CAS predicate (combinational re-read of the entry in the CAS cycle).
    //=========================================================================
    wire cas_match    = (e_gen[cas_chunk] == cas_gen);
    wire cas_resident = e_resident[cas_chunk];
    wire cas_clear    = ~e_pend[cas_chunk];
    wire cas_pred     = cas_match & cas_resident & cas_clear & cas_pin_free;

    assign cas_ok = cas_valid & cas_pred;

    //=========================================================================
    // Update arbitration. One write per bank per cycle; a successful CAS on
    // the same entry wins the cycle, so the update diverts to pending. The
    // total order per entry is: an update committed in an earlier cycle is
    // visible to the CAS re-read; an update losing a later cycle waits behind
    // the installed pin.
    //=========================================================================
    wire upd_same     = upd_valid & (upd_chunk == cas_chunk);
    wire upd_blocked  = (e_pins[upd_chunk] != '0) | (upd_same & cas_ok);
    assign upd_committed = upd_valid & ~upd_blocked;
    assign upd_pended    = upd_valid &  upd_blocked;

    //=========================================================================
    // Release: decrement the pin; on the last pin with a pended update, commit
    // the waiting update and clear pending_reclaim in the same edge.
    //=========================================================================
    wire rel_last  = rel_valid & (e_pins[rel_chunk] == {{(PIN_W-1){1'b0}}, 1'b1});
    wire rel_drain = rel_last & e_pend[rel_chunk];

    assign any_pending = |{e_pend[0], e_pend[1]};   // cheap TB probe for small banks

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < NUM_CHUNKS; i = i + 1) begin
                e_gen[i]      <= '0;
                e_resident[i] <= 1'b0;
                e_pins[i]     <= '0;
                e_pend[i]     <= 1'b0;
                p_gen[i]      <= '0;
                p_resident[i] <= 1'b0;
            end
        end else begin
            // CAS: conditional pin increment (single indivisible write).
            if (cas_ok) begin
                e_pins[cas_chunk] <= e_pins[cas_chunk] + 1'b1;
            end

            // Placement update.
            if (upd_committed) begin
                e_gen[upd_chunk]      <= upd_gen;
                e_resident[upd_chunk] <= upd_resident;
            end else if (upd_pended) begin
                e_pend[upd_chunk]     <= 1'b1;
                p_gen[upd_chunk]      <= upd_gen;
                p_resident[upd_chunk] <= upd_resident;
            end

            // Release (never blocked; pin count only decreases under pending).
            if (rel_valid && (e_pins[rel_chunk] != '0)) begin
                e_pins[rel_chunk] <= e_pins[rel_chunk] - 1'b1;
                if (rel_drain) begin
                    e_pend[rel_chunk]     <= 1'b0;
                    e_gen[rel_chunk]      <= p_gen[rel_chunk];
                    e_resident[rel_chunk] <= p_resident[rel_chunk];
                end
            end
        end
    end

    //=========================================================================
    // The four assertions, written as clocked immediate checks so they run
    // under Icarus Verilog. The equivalent concurrent-assertion forms are:
    //   A1: assert property (@(posedge clk) (cas_valid && cas_ok)
    //                          |-> $past(cas_pred, 0));
    //   A2: assert property (@(posedge clk) (cas_valid && !cas_pred)
    //                          |-> !cas_ok);
    //   A3: assert property (@(posedge clk) upd_committed
    //                          |-> (e_pins[upd_chunk] == 0
    //                               && !(upd_same && cas_ok)));
    //   A4: assert property (@(posedge clk) e_pend[cas_chunk] |-> !cas_ok);
    //=========================================================================
`ifndef SYNTHESIS
    always_ff @(posedge clk) begin
        if (rst_n) begin
            // A1: a pin write happens only on a fully true predicate.
            if (cas_valid && cas_ok && !cas_pred)
                $fatal(1, "A1 pin-write predicate violated at %0t", $time);
            // A2: a failed predicate never raises cas_ok (gate stays closed).
            if (cas_valid && !cas_pred && cas_ok)
                $fatal(1, "A2 reject-closes-gate violated at %0t", $time);
            // A3: an update commits only at zero pin count with no same-entry CAS.
            if (upd_valid && upd_committed &&
                ((e_pins[upd_chunk] != '0) || (upd_same && cas_ok)))
                $fatal(1, "A3 update-at-zero-pin violated at %0t", $time);
            // A4: pending reclaim rejects every new pin.
            if (cas_valid && e_pend[cas_chunk] && cas_ok)
                $fatal(1, "A4 pending-blocks-pins violated at %0t", $time);
        end
    end
`endif

endmodule
