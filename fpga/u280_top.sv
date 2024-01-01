//=============================================================================
// U280 APEX Prototyping Top — Alveo U280 FPGA Wrapper
//
// Wraps APEX_PIPELINE + CEFE front-end (vc_wrr, cfo_cam, bdb_parser) for
// prototyping on Xilinx Alveo U280 at 250 MHz.
//
// Features:
//   - Clock Wizard: 300 MHz differential input → 250 MHz system clock
//   - Async-assert / sync-deassert reset synchronizer
//   - AXI-Lite CSR interface (32-bit, 16 registers) for config/status
//   - PCIe-facing descriptor FIFO placeholder
//   - Full instantiation of scoring pipeline + CEFE modules
//
// Target: xcu280-fsvh2892-2L-e @ 250 MHz (4.0 ns period)
//=============================================================================
`timescale 1ns/1ps

module u280_apex_top (
    //=========================================================================
    // Differential System Clock (300 MHz from U280 SLR0 HBM refclk)
    //=========================================================================
    input  logic        sys_clk_p,
    input  logic        sys_clk_n,

    //=========================================================================
    // Active-low system reset (directly from PCIe perstn or board reset)
    //=========================================================================
    input  logic        sys_rst_n,

    //=========================================================================
    // PCIe Descriptor FIFO Interface (placeholder, directly to XDMA IP)
    //=========================================================================
    input  logic [127:0] pcie_desc_tdata,
    input  logic         pcie_desc_tvalid,
    output logic         pcie_desc_tready,
    input  logic         pcie_desc_tlast,

    output logic [127:0] pcie_cpl_tdata,
    output logic         pcie_cpl_tvalid,
    input  logic         pcie_cpl_tready,

    //=========================================================================
    // AXI-Lite CSR Interface (from XDMA BAR0)
    //=========================================================================
    input  logic [5:0]   s_axil_awaddr,
    input  logic         s_axil_awvalid,
    output logic         s_axil_awready,
    input  logic [31:0]  s_axil_wdata,
    input  logic [3:0]   s_axil_wstrb,
    input  logic         s_axil_wvalid,
    output logic         s_axil_wready,
    output logic [1:0]   s_axil_bresp,
    output logic         s_axil_bvalid,
    input  logic         s_axil_bready,
    input  logic [5:0]   s_axil_araddr,
    input  logic         s_axil_arvalid,
    output logic         s_axil_arready,
    output logic [31:0]  s_axil_rdata,
    output logic [1:0]   s_axil_rresp,
    output logic         s_axil_rvalid,
    input  logic         s_axil_rready,

    //=========================================================================
    // Status LEDs
    //=========================================================================
    output logic [1:0]   led_status
);

    //=========================================================================
    // Clock Generation: 300 MHz differential → 250 MHz system clock
    //
    // For Vivado synthesis: replace this block with a Clocking Wizard IP
    // configured as MMCM with:
    //   - Input:  300 MHz differential (IBUFDS)
    //   - Output: 250 MHz (CLKOUT0), locked output
    //
    // The simulation-friendly version below uses a simple IBUFDS + assign.
    //=========================================================================
    logic sys_clk_ibuf;
    logic apex_clk;      // 250 MHz system clock
    logic mmcm_locked;

    // Differential input buffer
    IBUFDS #(
        .DIFF_TERM    ("TRUE"),
        .IBUF_LOW_PWR ("FALSE")
    ) u_ibufds (
        .O  (sys_clk_ibuf),
        .I  (sys_clk_p),
        .IB (sys_clk_n)
    );

    // --- Vivado MMCM instantiation (replace for synthesis) ---
    // In simulation, pass through the input clock directly.
    // For synthesis, instantiate clk_wiz_0 or use the MMCME4_ADV primitive:
    //
    //   clk_wiz_0 u_clk_wiz (
    //       .clk_in1    (sys_clk_ibuf),
    //       .clk_out1   (apex_clk),       // 250 MHz
    //       .locked     (mmcm_locked),
    //       .reset      (~sys_rst_n)
    //   );
    //
    // Simulation placeholder:
    `ifdef SYNTHESIS
        // Instantiate Vivado Clock Wizard IP in actual build
        clk_wiz_0 u_clk_wiz (
            .clk_in1  (sys_clk_ibuf),
            .clk_out1 (apex_clk),
            .locked   (mmcm_locked),
            .reset    (~sys_rst_n)
        );
    `else
        assign apex_clk    = sys_clk_ibuf;  // Simulation: direct passthrough
        assign mmcm_locked = 1'b1;
    `endif

    //=========================================================================
    // Reset Synchronizer: Async Assert, Sync Deassert
    //
    // Asserts immediately on sys_rst_n deassertion or MMCM unlock.
    // Deasserts synchronously after 4-stage shift register stabilizes.
    //=========================================================================
    logic [3:0] rst_sync_pipe;
    logic       rst_n_sync;

    always_ff @(posedge apex_clk or negedge sys_rst_n) begin
        if (!sys_rst_n) begin
            rst_sync_pipe <= 4'b0000;
        end else begin
            rst_sync_pipe <= {rst_sync_pipe[2:0], mmcm_locked};
        end
    end

    assign rst_n_sync = rst_sync_pipe[3];

    //=========================================================================
    // AXI-Lite CSR Register Map (16 × 32-bit registers)
    //
    // Offset  | Name            | R/W | Description
    // --------|-----------------|-----|------------------------------------------
    // 0x00    | CTRL            | RW  | [0] pipeline enable, [1] flush, [2] clk_en
    // 0x04    | CFG_EPOCH       | RW  | [15:0] current epoch
    // 0x08    | CFG_NAMESPACE   | RW  | [7:0] current namespace
    // 0x0C    | CFG_ETA         | RW  | [2:0] quantized learning rate
    // 0x10    | CFG_EXPERT_MASK | RW  | [6:0] active expert mask
    // 0x14    | STAT_ADMITTED   | RO  | [31:0] total admitted count
    // 0x18    | STAT_REJECTED   | RO  | [31:0] total rejected count
    // 0x1C    | STAT_CYCLES     | RO  | [31:0] total cycle count
    // 0x20    | PIPELINE_STATUS | RO  | [0] idle, [1] mmcm_locked
    // 0x24    | CEFE_STATUS     | RO  | [4:0] cam_occupancy, [5] cam_full, [6] parse_busy
    // 0x28    | VERSION         | RO  | 32'hAPEX_0001
    // 0x2C    | SCRATCH         | RW  | General-purpose scratch register
    // 0x30-3C | Reserved        | --  | Reserved for future use
    //=========================================================================
    localparam int NUM_CSR = 16;

    logic [31:0] csr [0:NUM_CSR-1];

    // CSR field aliases (directly driven from register array)
    logic        csr_pipe_en;
    logic        csr_flush;
    logic        csr_clk_en;
    logic [15:0] csr_epoch;
    logic [7:0]  csr_namespace;
    logic [2:0]  csr_eta_q;
    logic [6:0]  csr_expert_mask;

    assign csr_pipe_en     = csr[0][0];
    assign csr_flush       = csr[0][1];
    assign csr_clk_en      = csr[0][2];
    assign csr_epoch       = csr[1][15:0];
    assign csr_namespace   = csr[2][7:0];
    assign csr_eta_q       = csr[3][2:0];
    assign csr_expert_mask = csr[4][6:0];

    // --- AXI-Lite Write Logic ---
    logic aw_fire, w_fire;

    assign aw_fire = s_axil_awvalid & s_axil_awready;
    assign w_fire  = s_axil_wvalid  & s_axil_wready;

    assign s_axil_awready = ~s_axil_bvalid | s_axil_bready;
    assign s_axil_wready  = ~s_axil_bvalid | s_axil_bready;

    always_ff @(posedge apex_clk or negedge rst_n_sync) begin
        if (!rst_n_sync) begin
            for (int i = 0; i < NUM_CSR; i++) csr[i] <= '0;
            csr[10] <= 32'hAPEX_001;  // VERSION register
        end else begin
            // Clear flush after one cycle (pulse behavior)
            if (csr[0][1]) csr[0][1] <= 1'b0;

            // AXI write
            if (aw_fire && w_fire) begin
                automatic int idx = s_axil_awaddr[5:2];
                // Only write to RW registers (0x00-0x10, 0x2C)
                if (idx <= 4 || idx == 11) begin
                    for (int b = 0; b < 4; b++) begin
                        if (s_axil_wstrb[b])
                            csr[idx][b*8 +: 8] <= s_axil_wdata[b*8 +: 8];
                    end
                end
            end

            // Status registers (RO, updated from hardware)
            csr[5]  <= stat_admitted_w;
            csr[6]  <= stat_rejected_w;
            csr[7]  <= stat_cycles_w;
            csr[8]  <= {30'b0, mmcm_locked, pipeline_idle_w};
            csr[9]  <= {25'b0, parse_busy_w, cam_full_w, cam_occupancy_w};
            csr[10] <= 32'hAPEX_001;
        end
    end

    // --- AXI-Lite Write Response ---
    always_ff @(posedge apex_clk or negedge rst_n_sync) begin
        if (!rst_n_sync) begin
            s_axil_bvalid <= 1'b0;
        end else begin
            if (aw_fire && w_fire)
                s_axil_bvalid <= 1'b1;
            else if (s_axil_bready)
                s_axil_bvalid <= 1'b0;
        end
    end
    assign s_axil_bresp = 2'b00;  // OKAY

    // --- AXI-Lite Read Logic ---
    always_ff @(posedge apex_clk or negedge rst_n_sync) begin
        if (!rst_n_sync) begin
            s_axil_rvalid <= 1'b0;
            s_axil_rdata  <= '0;
        end else begin
            if (s_axil_arvalid && s_axil_arready) begin
                s_axil_rvalid <= 1'b1;
                s_axil_rdata  <= csr[s_axil_araddr[5:2]];
            end else if (s_axil_rready) begin
                s_axil_rvalid <= 1'b0;
            end
        end
    end
    assign s_axil_arready = ~s_axil_rvalid | s_axil_rready;
    assign s_axil_rresp   = 2'b00;  // OKAY

    //=========================================================================
    // PCIe Descriptor FIFO (Placeholder)
    //
    // In production: XDMA AXI-Stream → synchronous FIFO → BDB parser doorbell.
    // For prototyping: direct passthrough with backpressure.
    //=========================================================================
    logic        doorbell_valid;
    logic        doorbell_ready;
    logic [47:0] doorbell_bdb_addr;
    logic [3:0]  doorbell_vc_id;

    // Extract doorbell fields from PCIe descriptor stream
    // Format: tdata[47:0] = BDB base address, tdata[51:48] = VC ID
    assign doorbell_valid    = pcie_desc_tvalid & pcie_desc_tlast;
    assign doorbell_bdb_addr = pcie_desc_tdata[47:0];
    assign doorbell_vc_id    = pcie_desc_tdata[51:48];
    assign pcie_desc_tready  = doorbell_ready;

    //=========================================================================
    // CEFE Module Instantiation
    //=========================================================================

    // --- BDB Parser ---
    logic        bdb_desc_valid;
    logic        bdb_desc_ready;
    logic [63:0] bdb_desc_handle;
    logic [7:0]  bdb_desc_priority;
    logic [15:0] bdb_desc_epoch;
    logic [7:0]  bdb_desc_namespace;
    logic [3:0]  bdb_desc_vc_id;
    logic [63:0] bdb_desc_hmac_tag;
    logic        parse_error_w;
    logic        parse_busy_w;
    logic [5:0]  bdb_desc_count;

    // DMA interface (tied off for prototyping — driven by ILA/VIO in hardware)
    logic         bdb_dma_req_valid;
    logic         bdb_dma_req_ready;
    logic [47:0]  bdb_dma_req_addr;
    logic [5:0]   bdb_dma_req_len;
    logic         bdb_dma_rsp_valid;
    logic [127:0] bdb_dma_rsp_data;
    logic         bdb_dma_rsp_last;
    logic         bdb_dma_rsp_error;

    cefe_bdb_parser #(
        .MAX_DESC_PER_BDB (64),
        .ADDR_WIDTH       (48),
        .DATA_WIDTH       (128),
        .HANDLE_WIDTH     (64),
        .TAG_WIDTH        (64)
    ) u_bdb_parser (
        .clk               (apex_clk),
        .rst_n             (rst_n_sync),
        .doorbell_valid    (doorbell_valid),
        .doorbell_ready    (doorbell_ready),
        .doorbell_bdb_addr (doorbell_bdb_addr),
        .doorbell_vc_id    (doorbell_vc_id),
        .dma_req_valid     (bdb_dma_req_valid),
        .dma_req_ready     (bdb_dma_req_ready),
        .dma_req_addr      (bdb_dma_req_addr),
        .dma_req_len       (bdb_dma_req_len),
        .dma_rsp_valid     (bdb_dma_rsp_valid),
        .dma_rsp_ready     (/* open */),
        .dma_rsp_data      (bdb_dma_rsp_data),
        .dma_rsp_last      (bdb_dma_rsp_last),
        .dma_rsp_error     (bdb_dma_rsp_error),
        .desc_valid        (bdb_desc_valid),
        .desc_ready        (bdb_desc_ready),
        .desc_chunk_handle (bdb_desc_handle),
        .desc_priority     (bdb_desc_priority),
        .desc_epoch        (bdb_desc_epoch),
        .desc_namespace    (bdb_desc_namespace),
        .desc_vc_id        (bdb_desc_vc_id),
        .desc_hmac_tag     (bdb_desc_hmac_tag),
        .parse_error       (parse_error_w),
        .parse_busy        (parse_busy_w),
        .desc_count_out    (bdb_desc_count)
    );

    // Tie off DMA interface for FPGA prototyping (connect to HBM AXI in full build)
    assign bdb_dma_req_ready = 1'b1;
    assign bdb_dma_rsp_valid = 1'b0;
    assign bdb_dma_rsp_data  = '0;
    assign bdb_dma_rsp_last  = 1'b0;
    assign bdb_dma_rsp_error = 1'b0;

    // --- CFO CAM ---
    logic        cam_full_w;
    logic [4:0]  cam_occupancy_w;

    // CFO CAM DMA interface (placeholder)
    logic        cfo_dma_rd_valid;
    logic        cfo_dma_rd_ready;
    logic [63:0] cfo_dma_rd_handle;
    logic [3:0]  cfo_dma_rd_entry_id;

    // Multicast completion (unused in prototyping — loopback)
    logic [15:0] mcast_cpl_valid_w;
    logic [63:0] mcast_cpl_handle_w;
    logic        mcast_cpl_error_w;

    cefe_cfo_cam #(
        .NUM_ENTRIES  (16),
        .HANDLE_WIDTH (64),
        .NUM_VC       (16),
        .TAG_WIDTH    (64)
    ) u_cfo_cam (
        .clk              (apex_clk),
        .rst_n            (rst_n_sync),
        .step_boundary    (csr_flush),   // Decode step boundary for EMA clock-gating
        .req_valid        (bdb_desc_valid),
        .req_ready        (bdb_desc_ready),
        .req_chunk_handle (bdb_desc_handle),
        .req_vc_id        (bdb_desc_vc_id),
        .req_hmac_tag     (bdb_desc_hmac_tag),
        // Promotion descriptors are read-only chunk fetches for the attention
        // kernel, so the coalesced region is read-only by construction here.
        .req_region_ro    (1'b1),
        // Bind the decode-step epoch so a stale (handle,tag) pair cannot be
        // replayed into a later step to force a coalesce.
        .req_epoch        (bdb_desc_epoch),
        // No CEFE-side SEA instance in this FPGA prototype (APEX_SEA lives
        // inside APEX_PIPELINE); tie the overlap-probe wake off. In the full
        // endpoint this connects to the SEA cross-tenant overlap detector.
        .sea_wake         (1'b0),
        .dma_rd_valid     (cfo_dma_rd_valid),
        .dma_rd_ready     (cfo_dma_rd_ready),
        .dma_rd_handle    (cfo_dma_rd_handle),
        .dma_rd_entry_id  (cfo_dma_rd_entry_id),
        .dma_cpl_valid    (1'b0),
        .dma_cpl_entry_id (4'b0),
        .dma_cpl_error    (1'b0),
        .mcast_cpl_valid  (mcast_cpl_valid_w),
        .mcast_cpl_handle (mcast_cpl_handle_w),
        .mcast_cpl_error  (mcast_cpl_error_w),
        .hmac_req_valid   (/* open — tie HMAC to always-pass for proto */),
        .hmac_req_ready   (1'b1),
        .hmac_req_tag     (/* open */),
        .hmac_req_handle  (/* open */),
        .hmac_rsp_valid   (1'b1),       // Always pass HMAC in prototyping
        .hmac_rsp_pass    (1'b1),
        .cam_occupancy    (cam_occupancy_w),
        .cam_full         (cam_full_w)
    );

    assign cfo_dma_rd_ready = 1'b1;  // Always accept DMA reads (loopback)

    // --- VC WRR Arbiter ---
    // For prototyping: single-VC mode (VC0 only), other VCs tied off
    logic                  wrr_pop_valid;
    logic                  wrr_pop_ready;
    logic [127:0]          wrr_pop_data;
    logic [3:0]            wrr_pop_vc_id;

    // Push interface: connect VC0 to PCIe descriptor stream (simplified)
    logic [15:0]           wrr_push_valid;
    logic [15:0]           wrr_push_ready;
    logic [127:0]          wrr_push_data [0:15];
    logic [3:0]            wrr_cfg_weight [0:15];

    // Connect PCIe descriptor to VC0 push port
    assign wrr_push_valid    = {15'b0, pcie_desc_tvalid & ~pcie_desc_tlast};
    assign wrr_push_data[0]  = pcie_desc_tdata;

    // Default weights: all VCs weight = 1
    generate
        for (genvar gv = 0; gv < 16; gv++) begin : gen_vc_defaults
            assign wrr_cfg_weight[gv] = 4'd1;
            if (gv > 0) assign wrr_push_data[gv] = '0;
        end
    endgenerate

    cefe_vc_wrr #(
        .NUM_VC      (16),
        .QUEUE_DEPTH (32),
        .DESC_WIDTH  (128),
        .WEIGHT_BITS (4)
    ) u_vc_wrr (
        .clk          (apex_clk),
        .rst_n        (rst_n_sync),
        .push_valid   (wrr_push_valid),
        .push_ready   (wrr_push_ready),
        .push_data    (wrr_push_data),
        .pop_valid    (wrr_pop_valid),
        .pop_ready    (wrr_pop_ready),
        .pop_data     (wrr_pop_data),
        .pop_vc_id    (wrr_pop_vc_id),
        .pipe_stall   (~csr_pipe_en),
        .cfg_weight   (wrr_cfg_weight),
        .cfg_vc_enable(16'hFFFF)
    );

    //=========================================================================
    // APEX Pipeline Instantiation
    //=========================================================================
    logic        pipeline_idle_w;
    logic [31:0] stat_admitted_w;
    logic [31:0] stat_rejected_w;
    logic [31:0] stat_cycles_w;

    // Map WRR pop data → pipeline command inputs
    // WRR pop_data format (128 bits): same as BDB descriptor format
    //   [127:119] chunk_id (9-bit), [118:103] epoch, [102:95] namespace, [94:87] priority
    logic [8:0]  pipe_cmd_chunk_id;
    logic [15:0] pipe_cmd_epoch;
    logic [7:0]  pipe_cmd_namespace;
    logic [7:0]  pipe_cmd_priority;

    assign pipe_cmd_chunk_id  = wrr_pop_data[127:119];
    assign pipe_cmd_epoch     = wrr_pop_data[118:103];
    assign pipe_cmd_namespace = wrr_pop_data[102:95];
    assign pipe_cmd_priority  = wrr_pop_data[94:87];

    // DMA and completion interfaces
    logic [8:0]  pipe_dma_chunk_id;
    logic [15:0] pipe_dma_score;
    logic        pipe_dma_valid;
    logic [8:0]  pipe_cpl_chunk_id;
    logic [1:0]  pipe_cpl_status;
    logic        pipe_cpl_valid;

    APEX_PIPELINE u_apex_pipeline (
        .clk               (apex_clk),
        .rst_n             (rst_n_sync),
        .clk_en            (csr_clk_en),

        // Command ring (from WRR arbiter)
        .cmd_chunk_id      (pipe_cmd_chunk_id),
        .cmd_epoch         (pipe_cmd_epoch),
        .cmd_namespace     (pipe_cmd_namespace),
        .cmd_priority      (pipe_cmd_priority),
        .cmd_valid         (wrr_pop_valid & csr_pipe_en),
        .cmd_ready         (wrr_pop_ready),

        // Completion ring (to PCIe)
        .cpl_chunk_id      (pipe_cpl_chunk_id),
        .cpl_status        (pipe_cpl_status),
        .cpl_valid         (pipe_cpl_valid),
        .cpl_ready         (pcie_cpl_tready),

        // DMA issue
        .dma_chunk_id      (pipe_dma_chunk_id),
        .dma_score         (pipe_dma_score),
        .dma_valid         (pipe_dma_valid),
        .dma_ready         (1'b1),  // Always ready in prototyping

        // Feedback (directly from CSR-driven test interface for proto)
        .fb_chunk_id       (9'b0),
        .fb_attention_mass (16'b0),
        .fb_expert_id      (3'b0),
        .fb_valid          (1'b0),
        // Configuration (from AXI-Lite CSRs)
        .cfg_current_epoch     (csr_epoch),
        .cfg_current_namespace (csr_namespace),
        .cfg_eta_q             (csr_eta_q),
        .cfg_flush             (csr_flush),
        .cfg_expert_active_mask(csr_expert_mask),
        .cfg_sea_enable        (1'b1),  // enable stochastic exploration in FPGA proto

        // Residency management (tied off for prototyping)
        .res_set_id        (9'b0),
        .res_set_valid     (1'b0),
        .res_clear_id      (9'b0),
        .res_clear_valid   (1'b0),

        // Status
        .pipeline_idle     (pipeline_idle_w),
        .stat_admitted     (stat_admitted_w),
        .stat_rejected     (stat_rejected_w),
        .stat_total_cycles (stat_cycles_w)
    );

    //=========================================================================
    // PCIe Completion Output Mapping
    //=========================================================================
    assign pcie_cpl_tdata  = {118'b0, pipe_cpl_status, pipe_cpl_chunk_id[7:0]};
    assign pcie_cpl_tvalid = pipe_cpl_valid;

    //=========================================================================
    // Status LEDs
    //   led_status[0] = MMCM locked (heartbeat)
    //   led_status[1] = pipeline active (not idle)
    //=========================================================================
    assign led_status[0] = mmcm_locked;
    assign led_status[1] = ~pipeline_idle_w;

endmodule
