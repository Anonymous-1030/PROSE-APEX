//=============================================================================
// CEFE BDB Parser — Batch Descriptor Block DMA Engine
//
// Implements the host-to-endpoint descriptor delivery from §5.1:
//   - Receives a doorbell trigger with BDB base address + count
//   - Issues a burst DMA read to fetch the entire BDB from host HBM
//   - Parses the BDB header (magic, version, count, tenant HMAC)
//   - Extracts individual descriptors (64-bit chunk handle, priority, epoch)
//   - Streams parsed descriptors one-per-cycle to the downstream CFO CAM
//   - Handles misaligned/corrupt BDB gracefully (error flag, drain)
//
// BDB Format (in host HBM):
//   Offset 0x00: [63:48] magic=0xCEFE | [47:32] version | [31:16] desc_count | [15:0] rsvd
//   Offset 0x08: [63:0] HMAC-SHA256 tag (truncated to 64 bits)
//   Offset 0x10: Descriptor[0] = {handle[63:0], priority[7:0], epoch[15:0], ns[7:0], rsvd[31:0]}
//   Offset 0x20: Descriptor[1] ...
//   ...
//   Each descriptor is 16 bytes (128 bits).
//
// Target: ASAP7 7nm @ 1 GHz
// Area estimate: 0.005 mm² (state machine + shift register)
//=============================================================================
`timescale 1ns/1ps

module cefe_bdb_parser #(
    parameter int MAX_DESC_PER_BDB = 64,
    parameter int ADDR_WIDTH       = 48,   // Physical address width
    parameter int DATA_WIDTH       = 128,  // DMA bus width (128-bit)
    parameter int HANDLE_WIDTH     = 64,
    parameter int TAG_WIDTH        = 64
)(
    input  logic                    clk,
    input  logic                    rst_n,

    //=========================================================================
    // Doorbell Interface (from MMIO decoder)
    //=========================================================================
    input  logic                    doorbell_valid,
    output logic                    doorbell_ready,
    input  logic [ADDR_WIDTH-1:0]   doorbell_bdb_addr,  // Base address of BDB in host HBM
    input  logic [3:0]              doorbell_vc_id,      // Which VC this host maps to

    //=========================================================================
    // DMA Read Master Interface (to CXL memory controller)
    //=========================================================================
    output logic                    dma_req_valid,
    input  logic                    dma_req_ready,
    output logic [ADDR_WIDTH-1:0]   dma_req_addr,
    output logic [5:0]              dma_req_len,   // Burst length in 128-bit beats

    input  logic                    dma_rsp_valid,
    output logic                    dma_rsp_ready,
    input  logic [DATA_WIDTH-1:0]   dma_rsp_data,
    input  logic                    dma_rsp_last,
    input  logic                    dma_rsp_error,

    //=========================================================================
    // Descriptor Output Interface (to CFO CAM)
    //=========================================================================
    output logic                    desc_valid,
    input  logic                    desc_ready,
    output logic [HANDLE_WIDTH-1:0] desc_chunk_handle,
    output logic [7:0]              desc_priority,
    output logic [15:0]             desc_epoch,
    output logic [7:0]              desc_namespace,
    output logic [3:0]              desc_vc_id,
    output logic [TAG_WIDTH-1:0]    desc_hmac_tag,     // BDB-level HMAC for all descs

    //=========================================================================
    // Error / Status
    //=========================================================================
    output logic                    parse_error,       // BDB format error
    output logic                    parse_busy,
    output logic [5:0]              desc_count_out     // Number of descriptors in current BDB
);

    //=========================================================================
    // BDB Header Constants
    //=========================================================================
    localparam logic [15:0] BDB_MAGIC   = 16'hCEFE;
    localparam logic [15:0] BDB_VERSION = 16'h0001;

    //=========================================================================
    // FSM States
    //=========================================================================
    typedef enum logic [2:0] {
        ST_IDLE,        // Waiting for doorbell
        ST_DMA_REQ,     // Issue DMA read request for BDB
        ST_HDR_RECV,    // Receiving BDB header (first 128-bit beat)
        ST_TAG_RECV,    // Receiving HMAC tag (second 128-bit beat: tag in lower 64)
        ST_DESC_RECV,   // Receiving descriptor beats
        ST_DESC_EMIT,   // Emitting parsed descriptor downstream
        ST_ERROR        // Error state (drain remaining DMA data)
    } fsm_t;

    fsm_t fsm, fsm_next;

    //=========================================================================
    // Internal Registers
    //=========================================================================
    logic [ADDR_WIDTH-1:0] bdb_addr_r;
    logic [3:0]            vc_id_r;
    logic [15:0]           desc_count_r;     // From BDB header
    logic [5:0]            desc_idx;         // Current descriptor index
    logic [TAG_WIDTH-1:0]  hmac_tag_r;       // Extracted HMAC tag
    logic [DATA_WIDTH-1:0] desc_beat_r;      // Latched descriptor beat
    logic                  header_valid_r;

    // Helper extracts for constant part-selects; Icarus does not support
    // constant selects inside always_* processes, so drive them with assigns.
    logic [15:0] dma_rsp_magic;
    logic [15:0] dma_rsp_version;
    logic [15:0] dma_rsp_desc_count;
    logic [63:0] dma_rsp_lower64;
    logic [5:0]  desc_count_limit;

    assign dma_rsp_magic     = dma_rsp_data[127:112];
    assign dma_rsp_version   = dma_rsp_data[111:96];
    assign dma_rsp_desc_count= dma_rsp_data[95:80];
    assign dma_rsp_lower64   = dma_rsp_data[63:0];
    assign desc_count_limit  = desc_count_r[5:0] - 6'd1;

    //=========================================================================
    // FSM Register
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            fsm <= ST_IDLE;
        else
            fsm <= fsm_next;
    end

    //=========================================================================
    // Next-State Logic
    //=========================================================================
    always_comb begin
        fsm_next = fsm;
        case (fsm)
            ST_IDLE: begin
                if (doorbell_valid && doorbell_ready)
                    fsm_next = ST_DMA_REQ;
            end
            ST_DMA_REQ: begin
                if (dma_req_ready)
                    fsm_next = ST_HDR_RECV;
            end
            ST_HDR_RECV: begin
                if (dma_rsp_valid) begin
                    // Validate magic
                    if (dma_rsp_magic == BDB_MAGIC)
                        fsm_next = ST_TAG_RECV;
                    else
                        fsm_next = ST_ERROR;
                end
            end
            ST_TAG_RECV: begin
                if (dma_rsp_valid)
                    fsm_next = ST_DESC_RECV;
            end
            ST_DESC_RECV: begin
                if (dma_rsp_valid) begin
                    if (dma_rsp_error)
                        fsm_next = ST_ERROR;
                    else
                        fsm_next = ST_DESC_EMIT;
                end
            end
            ST_DESC_EMIT: begin
                if (desc_ready) begin
                    if (desc_idx >= desc_count_limit)
                        fsm_next = ST_IDLE;  // All descriptors emitted
                    else
                        fsm_next = ST_DESC_RECV;  // More descriptors to receive
                end
            end
            ST_ERROR: begin
                // Drain any remaining DMA responses
                if (dma_rsp_valid && dma_rsp_last)
                    fsm_next = ST_IDLE;
            end
            default: fsm_next = ST_IDLE;
        endcase
    end

    //=========================================================================
    // Doorbell Latch
    //=========================================================================
    assign doorbell_ready = (fsm == ST_IDLE);
    assign parse_busy     = (fsm != ST_IDLE);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            bdb_addr_r   <= '0;
            vc_id_r      <= '0;
        end else if (fsm == ST_IDLE && doorbell_valid) begin
            bdb_addr_r   <= doorbell_bdb_addr;
            vc_id_r      <= doorbell_vc_id;
        end
    end

    //=========================================================================
    // DMA Request Generation
    //
    // We read the entire BDB in one burst. Maximum BDB size:
    //   Header (1 beat) + Tag (1 beat) + 64 descriptors (64 beats) = 66 beats
    // We use the maximum initially; actual length is refined once header is read.
    // For simplicity, issue full 66-beat burst (safe: host guarantees BDB is valid).
    //=========================================================================
    localparam int MAX_BDB_BEATS = 2 + MAX_DESC_PER_BDB;  // header + tag + descs

    assign dma_req_valid = (fsm == ST_DMA_REQ);
    assign dma_req_addr  = bdb_addr_r;
    assign dma_req_len   = 6'(MAX_BDB_BEATS);

    //=========================================================================
    // DMA Response Accept (always accept unless in EMIT state waiting downstream)
    //=========================================================================
    assign dma_rsp_ready = (fsm == ST_HDR_RECV) ||
                           (fsm == ST_TAG_RECV) ||
                           (fsm == ST_DESC_RECV) ||
                           (fsm == ST_ERROR);

    //=========================================================================
    // Header Parsing
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            desc_count_r   <= '0;
            header_valid_r <= 1'b0;
        end else if (fsm == ST_HDR_RECV && dma_rsp_valid) begin
            // BDB header format (128-bit beat):
            // [127:112] magic  [111:96] version  [95:80] desc_count  [79:0] reserved
            desc_count_r   <= dma_rsp_desc_count;
            header_valid_r <= (dma_rsp_magic == BDB_MAGIC) &&
                              (dma_rsp_version == BDB_VERSION);
        end
    end

    assign desc_count_out = desc_count_r[5:0];

    //=========================================================================
    // HMAC Tag Extraction
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            hmac_tag_r <= '0;
        end else if (fsm == ST_TAG_RECV && dma_rsp_valid) begin
            // Tag beat: [63:0] = HMAC-SHA256 truncated tag (lower 64 bits of beat)
            hmac_tag_r <= dma_rsp_lower64;
        end
    end

    //=========================================================================
    // Descriptor Beat Latch + Index Counter
    //=========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            desc_beat_r <= '0;
            desc_idx    <= '0;
        end else begin
            if (fsm == ST_IDLE) begin
                desc_idx <= '0;
            end else if (fsm == ST_DESC_RECV && dma_rsp_valid) begin
                desc_beat_r <= dma_rsp_data;
            end else if (fsm == ST_DESC_EMIT && desc_ready) begin
                desc_idx <= desc_idx + 6'd1;
            end
        end
    end

    //=========================================================================
    // Descriptor Output: parse fields from latched 128-bit beat
    //
    // Descriptor format (128 bits):
    //   [127:64] chunk_handle
    //   [63:56]  priority
    //   [55:40]  epoch
    //   [39:32]  namespace
    //   [31:0]   reserved
    //=========================================================================
    assign desc_valid        = (fsm == ST_DESC_EMIT);
    assign desc_chunk_handle = desc_beat_r[127:64];
    assign desc_priority     = desc_beat_r[63:56];
    assign desc_epoch        = desc_beat_r[55:40];
    assign desc_namespace    = desc_beat_r[39:32];
    assign desc_vc_id        = vc_id_r;
    assign desc_hmac_tag     = hmac_tag_r;

    //=========================================================================
    // Error Flag
    //=========================================================================
    logic parse_error_r;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            parse_error_r <= 1'b0;
        end else begin
            if (fsm == ST_IDLE)
                parse_error_r <= 1'b0;
            else if (fsm == ST_ERROR)
                parse_error_r <= 1'b1;
        end
    end

    assign parse_error = parse_error_r;

endmodule
