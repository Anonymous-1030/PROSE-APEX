//=============================================================================
// CEFE Mode A Endpoint DMA Engine  (paper §III-D, Table I; LIMITATIONS §5)
//
// Mode A ("push") data path: after the OAT admits a descriptor and installs its
// transfer-lifetime pin, this engine issues P2P posted memory writes to a GPU
// BAR and, on completion, drives RELEASE(d) so the pin is held for exactly the
// transfer's duration (Invariant 1, Theorem 1(c): "the hold is not released
// before the final payload transaction completes").
//
// Realized here (behavioral, synthesizable):
//   * PCIe/CXL posted-write TLP formatting: a chunk of `chunk_beats` flits is
//     streamed as posted writes to gpu_bar_base + offset. Posted => no
//     completion TLP, matching the non-preemptive/irreversible payload path.
//   * PASID / IOMMU tag insertion: each TLP carries the descriptor's PASID for
//     isolation (tlp_pasid), as required for GPU-directed P2P.
//   * Credit-based flow control: writes stall when posted-write credits are
//     exhausted (credit_avail), returned by the GPU BAR / root complex.
//   * Completion tracking tied to pin release: when the last beat of a chunk is
//     accepted, xfer_done pulses with the (chunk,gen) so the pipeline issues
//     RELEASE(d). The pin is thus scoped to [ISSUE, COMPLETE).
//
// This engine is the Mode A endpoint silicon whose absence made the 3.1x/5.9x
// ratios projections. It does not change the admission invariant (identical
// across Mode A/B); it only defines how the payload reaches the GPU after the
// pin is installed. Timing/area of the TLP formatter are a separate synthesis
// concern; this RTL establishes functional correctness and the pin-scoped
// completion contract.
//
// Target: ASAP7 7nm @ 1 GHz.
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module cefe_dma_engine #(
    parameter int CHUNK_W    = 9,
    parameter int GEN_W      = 16,
    parameter int PASID_W    = 20,
    parameter int ADDR_W     = 48,
    parameter int BEAT_W     = 256,      // CXL/PCIe flit payload width
    parameter int MAX_BEATS  = 16        // beats per chunk (64-token KV chunk)
) (
    input  logic                 clk,
    input  logic                 rst_n,

    //-------------------------------------------------------------------------
    // Admitted-descriptor input (from S7 of APEX_PIPELINE, post-OAT-pin).
    //-------------------------------------------------------------------------
    input  logic                 admit_valid,     // a pinned, admitted descriptor
    input  logic [CHUNK_W-1:0]   admit_chunk,
    input  logic [GEN_W-1:0]     admit_gen,
    input  logic [PASID_W-1:0]   admit_pasid,
    input  logic [ADDR_W-1:0]    admit_gpu_addr,  // GPU BAR target for this chunk
    input  logic [4:0]           admit_beats,     // beats to move (1..MAX_BEATS)
    output logic                 admit_ready,     // engine can accept a new chunk

    //-------------------------------------------------------------------------
    // P2P posted-write TLP stream (to GPU BAR via PCIe/CXL).
    //-------------------------------------------------------------------------
    output logic                 tlp_valid,
    output logic [ADDR_W-1:0]    tlp_addr,
    output logic [BEAT_W-1:0]    tlp_data,
    output logic [PASID_W-1:0]   tlp_pasid,       // IOMMU/PASID isolation tag
    output logic                 tlp_last,        // last beat of this chunk
    input  logic                 tlp_ready,       // downstream can accept a beat

    //-------------------------------------------------------------------------
    // Credit-based flow control (posted-write credits from root complex / BAR).
    //-------------------------------------------------------------------------
    input  logic                 credit_avail,    // >=1 posted-write credit free

    //-------------------------------------------------------------------------
    // Payload source (endpoint-local staging; metadata-only endpoint reads KV
    // bytes from the pool frame — modeled here as an opaque data feed).
    //-------------------------------------------------------------------------
    input  logic [BEAT_W-1:0]    src_data,

    //-------------------------------------------------------------------------
    // Completion -> RELEASE(d). Pulses when the final beat of a chunk is
    // accepted; the pipeline decrements the pin for (chunk, gen).
    //-------------------------------------------------------------------------
    output logic                 xfer_done,
    output logic [CHUNK_W-1:0]   xfer_chunk,
    output logic [GEN_W-1:0]     xfer_gen,

    //-------------------------------------------------------------------------
    // Status
    //-------------------------------------------------------------------------
    output logic                 busy,
    output logic [31:0]          stat_chunks_done,
    output logic [31:0]          stat_beats_sent
);

    typedef enum logic [1:0] { IDLE, STREAM, DONE } state_t;
    state_t state;

    logic [CHUNK_W-1:0] cur_chunk;
    logic [GEN_W-1:0]   cur_gen;
    logic [PASID_W-1:0] cur_pasid;
    logic [ADDR_W-1:0]  cur_addr;
    logic [4:0]         beats_left;
    logic [4:0]         beat_idx;

    assign admit_ready = (state == IDLE);
    assign busy        = (state != IDLE);

    // A beat is issued when we have data to send, downstream is ready, AND a
    // posted-write credit is available (credit-based flow control).
    wire beat_fire = (state == STREAM) & tlp_ready & credit_avail;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state          <= IDLE;
            tlp_valid      <= 1'b0;
            tlp_last       <= 1'b0;
            xfer_done      <= 1'b0;
            beats_left     <= '0;
            beat_idx       <= '0;
            stat_chunks_done <= '0;
            stat_beats_sent  <= '0;
        end else begin
            xfer_done <= 1'b0;   // one-cycle pulse by default

            case (state)
                IDLE: begin
                    tlp_valid <= 1'b0;
                    tlp_last  <= 1'b0;
                    if (admit_valid) begin
                        cur_chunk  <= admit_chunk;
                        cur_gen    <= admit_gen;
                        cur_pasid  <= admit_pasid;
                        cur_addr   <= admit_gpu_addr;
                        beats_left <= (admit_beats == 0) ? 5'd1 : admit_beats;
                        beat_idx   <= '0;
                        state      <= STREAM;
                    end
                end

                STREAM: begin
                    // Present the current beat; it commits when beat_fire.
                    tlp_valid <= credit_avail;   // gated by credit availability
                    tlp_addr  <= cur_addr;
                    tlp_data  <= src_data;
                    tlp_pasid <= cur_pasid;
                    tlp_last  <= (beats_left == 5'd1);

                    if (beat_fire) begin
                        stat_beats_sent <= stat_beats_sent + 1'b1;
                        cur_addr   <= cur_addr + (BEAT_W/8);
                        beat_idx   <= beat_idx + 5'd1;
                        if (beats_left == 5'd1) begin
                            // Last beat accepted -> transfer complete.
                            tlp_valid <= 1'b0;
                            tlp_last  <= 1'b0;
                            state     <= DONE;
                        end else begin
                            beats_left <= beats_left - 5'd1;
                        end
                    end
                    // If no credit, hold (tlp_valid deasserted) — flow control.
                end

                DONE: begin
                    // Fire the completion -> RELEASE(d). Pin held across the
                    // whole [ISSUE, COMPLETE) interval (Theorem 1(c)).
                    xfer_done  <= 1'b1;
                    xfer_chunk <= cur_chunk;
                    xfer_gen   <= cur_gen;
                    stat_chunks_done <= stat_chunks_done + 1'b1;
                    state      <= IDLE;
                end

                default: state <= IDLE;
            endcase
        end
    end

endmodule
