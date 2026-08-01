//=============================================================================
// APEX Pipeline — Top-Level 9-Stage Admission Pipeline
// Processes CXL promotion descriptors through:
//   S1: Descriptor dequeue (1 cycle)
//   S2: PCM validation (2 cycles) — reject bypass exits here (4-cycle path)
//   S3: Expert bank read (1 cycle, 7 parallel banks)
//   S4: MAC accumulation (1 cycle, 7-wide weighted sum)
//   S5: Dual-zone exact top-K (2 cycles):
//       S5a — classify via parallel compare against ez_min and safe_min
//       S5b — execute three-branch admission
//       7-entry EZ (min-heap, depth 2) + 18-entry SZ (flat, min-tree)
//       Case 2 (cross-zone) asserts a 1-cycle stall so the next descriptor is
//       not classified against a stale safe_min (paper §5.3, restored).
//   S6: Weight update (overlapped, off critical path)
//   OAT pin: the Object Admission Transaction is consult, then CAS (paper
//       Section IV-B). The S2 validation cycles only collect advisory
//       information. The final commit stage re-reads the directory entry in
//       one indivisible compare-and-swap (cefe_directory): MAP[chunk] must
//       still equal <descriptor generation, resident> with no pending
//       reclaim, and only then is the pin count of the entry incremented.
//       That edge is the linearization point of the transaction. The old pin
//       table survives as the per-transfer in-flight index, written only on a
//       successful CAS. A validated descriptor whose CAS fails rejects with
//       zero payload. Placement updates share the directory write port with
//       the CAS (one write per cycle), so all writes to an entry form a
//       physical total order.
//   S7: DMA issue (1 cycle)
//   S8: MMIO completion register (admit-only 9th stage) — registers the
//       admitted completion one extra cycle so the admit path is exactly
//       9 cycles end-to-end (S1..S7 + S8). The reject/bypass path writes the
//       completion directly and remains 4 cycles.
//
// Latency (measured S1-accept -> cpl_valid):
//   Admit path : 9 cycles (S1 + S2a + S2b + S3 + S4 + S5a + S5b + S7 + S8)
//   Reject path: 4 cycles (S1 + S2a + S2b + bypass completion register)
//
// Clock gating:
//   - Global clk_en ICG (coarse).
//   - Per-stage ICGs for S3/S4 datapath + heap(S5)/weight(S6): a descriptor
//     that is rejected at S2 never toggles S3-S6 because those stages' clock
//     enables are activity-driven (only clock when valid work is present).
//     NOTE: this is implemented as safe activity-based gating rather than a
//     blunt "reject_bypass forces clk_en=0 on S3-S6", because S3-S6 are shared
//     and may hold a valid *admit* descriptor when a later reject appears at
//     S2 — force-gating on reject_bypass would corrupt that in-flight admit.
//   - Per-bank ICGs: in Core2 (active mask = 2 experts) the 5 inactive banks
//     are clock-gated to retention; their (zero-weighted) lanes do not affect
//     the score.
//
// cfg_flush drain protocol: flush gated by pipeline_idle (weight/heap updates
// occur only in the drained window). Feedback writes to expert banks are also
// gated by pipeline_idle to preserve the t-1 causal boundary.
//
// Target: ASAP7 7nm @ 1GHz
// Area estimate (asic/reports/area.rpt, structural, register-file banks):
//   ~0.024 mm² with routing. NOTE: earlier "0.100 mm²" claim was the full
//   7-bank reserved config target; the estimated scoring-pipeline area is
//   0.024 mm². Full-endpoint 1.145 mm² is a projection, not yet synthesized.
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_PIPELINE (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        clk_en,

    //=========================================================================
    // Command Ring Interface (MMIO, from GPU runtime)
    //=========================================================================
    input  logic [8:0]  cmd_chunk_id,
    input  logic [15:0] cmd_epoch,
    input  logic [7:0]  cmd_namespace,
    input  logic [7:0]  cmd_priority,
    input  logic        cmd_valid,
    output logic        cmd_ready,

    //=========================================================================
    // Completion Ring Interface (to GPU runtime)
    //=========================================================================
    output logic [8:0]  cpl_chunk_id,
    output logic [1:0]  cpl_status,       // 2'b01=ADMITTED, 2'b10=REJECTED
    output logic        cpl_valid,
    input  logic        cpl_ready,

    //=========================================================================
    // DMA Issue Interface (to endpoint copy engine)
    //=========================================================================
    output logic [8:0]  dma_chunk_id,
    output logic [15:0] dma_score,
    output logic        dma_valid,
    input  logic        dma_ready,

    //=========================================================================
    // Feedback Interface (from GPU, asynchronous to scoring path)
    //=========================================================================
    input  logic [8:0]  fb_chunk_id,
    input  logic [15:0] fb_attention_mass,
    input  logic [2:0]  fb_expert_id,       // Which expert to update
    input  logic        fb_valid,

    //=========================================================================
    // Configuration (quasi-static, set per decode step)
    //=========================================================================
    input  logic [15:0] cfg_current_epoch,
    input  logic [7:0]  cfg_current_namespace,
    input  logic [2:0]  cfg_eta_q,          // Quantized learning rate
    input  logic        cfg_flush,          // Flush heap for new decode step
    input  logic [6:0]  cfg_expert_active_mask,  // Which experts are active
    input  logic        cfg_sea_enable,            // SEA probe enable (0 for deterministic xcheck)

    //=========================================================================
    // Residency management
    //=========================================================================
    input  logic [8:0]  res_set_id,
    input  logic        res_set_valid,
    input  logic [8:0]  res_clear_id,
    input  logic        res_clear_valid,

    //=========================================================================
    // Object-directory reclaim probe (paper §III-B, Invariant 1)
    // The directory MUST consult reclaim_allowed before evicting/reusing a slot.
    // reclaim_allowed is low while any in-flight transfer pins (chunk,gen).
    //=========================================================================
    input  logic [8:0]  reclaim_chunk_id,
    input  logic [15:0] reclaim_generation,
    output logic        reclaim_allowed,

    //=========================================================================
    // Status / Statistics
    //=========================================================================
    output logic        pipeline_idle,      // All stages drained, safe to flush
    output logic [31:0] stat_admitted,
    output logic [31:0] stat_rejected,
    output logic [31:0] stat_total_cycles,
    output logic        stat_pin_reject,    // pulses when OAT rejects on full pin table
    output logic [9:0]  stat_pin_count      // live pins (transfer-lifetime holds)
);

    //=========================================================================
    // Clock Gating
    //   gated_clk    : coarse global gate (clk_en) — used by control/heap/weight
    //   s34_clk      : S3 (expert read reg) + S4 (MAC) datapath — gated when no
    //                  validated descriptor is advancing into scoring
    //   bank_clk[g]  : per-expert-bank gate (Core2 retention, see S3)
    // Activity-based enables are declared after the stage signals they depend
    // on; the ICGs are instantiated below those assigns.
    //=========================================================================
    logic gated_clk;
    ICG icg_inst (
        .CK  (clk),
        .E   (clk_en),
        .SE  (1'b0),
        .GCK (gated_clk)
    );

    // Per-stage gated clock for S3/S4 datapath (enable assigned after the
    // stage-signal declarations below).
    logic s34_clk;
    logic s34_en;

    //=========================================================================
    // Internal signals (parameters from apex_pkg)
    //=========================================================================

    // PCM outputs
    logic        pcm_pass, pcm_reject, pcm_valid;
    logic [8:0]  pcm_chunk_id;
    logic [15:0] pcm_epoch;

    // Expert bank read outputs
    logic [15:0] expert_pred [0:NUM_EXPERTS-1];
    logic        expert_rd_valid;
    logic [8:0]  s3_chunk_id;

    // MAC outputs
    logic [15:0] mac_score;
    logic [8:0]  mac_chunk_id;
    logic [15:0] mac_epoch;
    logic        mac_valid;

    // Heap outputs
    logic        heap_admitted, heap_admitted_valid;
    logic        heap_idle;

    // Weight outputs
    logic [NUM_EXPERTS-1:0][7:0] expert_weights;

    // Controller signals
    logic        pipe_stall, reject_bypass;
    logic        s7_issue_dma, s7_null_cpl;
    logic        bypass_cpl_valid;
    logic [8:0]  bypass_chunk_id;
    logic        case2_stall;   // 1-cycle Case 2 cross-zone stall from heap

    // cfg_flush drain protocol: gate flush behind pipeline_idle
    logic        cfg_flush_gated;

    //=========================================================================
    // Pipeline Idle Detection (for cfg_flush drain protocol)
    // All stage valids must be deasserted AND heap must be idle
    //=========================================================================
    assign pipeline_idle = ~s1_valid & ~pcm_valid & ~expert_rd_valid
                         & ~mac_valid & ~heap_admitted_valid & ~dma_valid
                         & heap_idle;

    // Gate cfg_flush: only allow when pipeline is fully drained
    assign cfg_flush_gated = cfg_flush & pipeline_idle;

    //=========================================================================
    // Per-stage clock enable (activity-based) for S3/S4 scoring datapath.
    //   The S3 output register and the S4 MAC array (the dominant dynamic-power
    //   toggler, see asic/reports/power.rpt) are clocked ONLY when a
    //   PCM-passed descriptor is entering or already traversing S3/S4. A
    //   descriptor rejected at S2 has pcm_pass=0, so it never raises s34_en and
    //   therefore never toggles S3/S4 — this delivers the reject-path dynamic
    //   power saving. The enable is a strict superset of the cycles S3/S4 must
    //   capture (pcm_pass -> expert_rd_valid -> mac_valid chain), so no
    //   descriptor is ever dropped. The heap (S5) and weight update (S6) stay
    //   on the global clock because they run multi-cycle autonomous FSMs
    //   (heapify, 24-cycle divide) that must not be frozen by activity gating.
    //=========================================================================
    assign s34_en = clk_en & (oat_pass | expert_rd_valid | mac_valid);
    ICG icg_s34 (.CK(clk), .E(s34_en), .SE(1'b0), .GCK(s34_clk));

    //=========================================================================
    // S1: Descriptor Dequeue (1 cycle)
    // Register the incoming descriptor. When cmd port is idle and SEA fires
    // a probe, inject a random chunk ID for exploration (probe does NOT
    // suppress real commands — it only fills idle slots).
    //=========================================================================
    logic [8:0]  s1_chunk_id;
    logic [15:0] s1_epoch;
    logic [7:0]  s1_namespace;
    logic        s1_valid;
    logic        s1_is_probe;  // Tag: 1 = SEA probe, 0 = real descriptor

    assign cmd_ready = ~pipe_stall;

    // SEA probe: already conditioned on pipe_idle inside APEX_SEA module.
    // This gate is a structural safety belt — probe_inject can only assert
    // when pipe_idle is high, so this AND is logically redundant but ensures
    // no probe ever preempts a real descriptor regardless of SEA bugs.
    wire sea_probe_valid = sea_probe & ~cmd_valid & ~pipe_stall;

    always_ff @(posedge gated_clk or negedge rst_n) begin
        if (!rst_n) begin
            s1_valid    <= 1'b0;
            s1_is_probe <= 1'b0;
        end else if (!pipe_stall) begin
            if (cmd_valid) begin
                // Real descriptor takes priority
                s1_valid     <= 1'b1;
                s1_chunk_id  <= cmd_chunk_id;
                s1_epoch     <= cmd_epoch;
                s1_namespace <= cmd_namespace;
                s1_is_probe  <= 1'b0;
            end else if (sea_probe_valid) begin
                // SEA probe: inject random chunk for exploration
                s1_valid     <= 1'b1;
                s1_chunk_id  <= sea_probe_chunk_id;
                s1_epoch     <= cfg_current_epoch;      // Use current epoch (pass PCM)
                s1_namespace <= cfg_current_namespace;  // Use current namespace (pass PCM)
                s1_is_probe  <= 1'b1;
            end else begin
                s1_valid    <= 1'b0;
                s1_is_probe <= 1'b0;
            end
        end
    end

    //=========================================================================
    // S2: PCM Validation (2 cycles)
    //=========================================================================
    APEX_PCM u_pcm (
        .clk                 (gated_clk),
        .rst_n               (rst_n),
        .stall               (pipe_stall),
        .desc_chunk_id       (s1_chunk_id),
        .desc_epoch          (s1_epoch),
        .desc_namespace      (s1_namespace),
        .desc_valid          (s1_valid),
        .cfg_current_epoch   (cfg_current_epoch),
        .cfg_current_namespace(cfg_current_namespace),
        .pcm_pass            (pcm_pass),
        .pcm_reject          (pcm_reject),
        .pcm_valid           (pcm_valid),
        .pcm_chunk_id_out    (pcm_chunk_id),
        .pcm_epoch_out       (pcm_epoch),
        .res_set_id          (res_set_id),
        .res_set_valid       (res_set_valid),
        .res_clear_id        (res_clear_id),
        .res_clear_valid     (res_clear_valid)
    );

    //=========================================================================
    // OAT consult-then-CAS (paper Section IV-B)
    //
    // Advisory result: the PCM pass only carries information. The descriptor
    // proceeds into scoring on pcm_pass alone; pin availability at S2 is an
    // advisory early-out, and the binding decision is made by the directory
    // CAS at the commit stage (S7).
    //
    // The CAS (cefe_directory) re-reads the entry in the commit cycle and
    // checks MAP[chunk] == <descriptor epoch, resident> with no pending
    // reclaim and a free in-flight index entry. On a full match it increments
    // the entry's pin count and, in the same edge, updates the in-flight
    // index (cefe_pin_table). A failed CAS retires the descriptor through the
    // heap-reject completion with zero payload.
    //=========================================================================
    localparam int PIN_ENTRIES = 400;

    logic                 pin_alloc_ok;
    logic                 pin_reject;       // advisory: validated, no free index entry
    logic                 oat_pass;         // advisory pass into scoring
    logic [$clog2(PIN_ENTRIES+1)-1:0] pin_count_w;
    logic                 pin_tab_full;

    // Tenant id proxy: the namespace field carries the tenant/VC id.
    wire [3:0] pcm_tenant = cfg_current_namespace[3:0];

    // The commit-stage CAS event and its verdict.
    wire s7_commit    = heap_admitted_valid & heap_admitted & ~drain_stall;
    wire dir_cas_ok;

    // Allocation fires only on a successful CAS (the linearization point).
    // The in-flight index carries no authority; it tracks the transfer.
    wire pin_alloc_fire = dir_cas_ok;

    cefe_pin_table #(.NUM_ENTRIES(PIN_ENTRIES)) u_pin_table (
        .clk            (gated_clk),
        .rst_n          (rst_n),
        .alloc_valid    (pin_alloc_fire),
        .alloc_tenant   (pcm_tenant),
        .alloc_chunk    (mac_chunk_id),
        .alloc_gen      (mac_epoch),
        .alloc_ok       (pin_alloc_ok),
        .alloc_index    (),
        // RELEASE(d) one cycle after the CAS, matching the evaluated
        // short-transfer case (drain-scoped release is the cefe_dma_engine
        // extension, see that module and LIMITATIONS). Fires only on a
        // CAS-installed pin, so alloc and release stay 1:1 per descriptor.
        .release_valid  (pin_release_valid),
        .release_chunk  (pin_release_chunk),
        .release_gen    (pin_release_gen),
        // Advisory probe port retained for the fabric manager; the
        // authoritative answer comes from the directory below.
        .reclaim_chunk  (reclaim_chunk_id),
        .reclaim_gen    (reclaim_generation),
        .reclaim_allowed(),
        .pin_count      (pin_count_w),
        .table_full     (pin_tab_full)
    );

    //=========================================================================
    // Object directory (authoritative mapping, consult-then-CAS).
    //   Update port: placement writes and reclaims share the bank write port
    //   with the CAS, one write per cycle per entry.
    //=========================================================================
    logic [2:0] dir_adv_pins;
    logic       dir_adv_pend;

    wire        upd_valid_w    = res_set_valid | res_clear_valid;
    wire [8:0]  upd_chunk_w    = res_clear_valid ? res_clear_id : res_set_id;
    wire [15:0] upd_gen_w      = cfg_current_epoch;
    wire        upd_resident_w = res_set_valid;

    cefe_directory #(.NUM_CHUNKS(NUM_CHUNKS)) u_directory (
        .clk            (gated_clk),
        .rst_n          (rst_n),
        .adv_chunk      (reclaim_chunk_id),
        .adv_gen        (),
        .adv_resident   (),
        .adv_pending    (dir_adv_pend),
        .adv_pin_count  (dir_adv_pins),
        .cas_valid      (s7_commit),
        .cas_chunk      (mac_chunk_id),
        .cas_gen        (mac_epoch),
        .cas_pin_free   (pin_alloc_ok),
        .cas_ok         (dir_cas_ok),
        .upd_valid      (upd_valid_w),
        .upd_chunk      (upd_chunk_w),
        .upd_gen        (upd_gen_w),
        .upd_resident   (upd_resident_w),
        .upd_committed  (),
        .upd_pended     (),
        .rel_valid      (pin_release_valid),
        .rel_chunk      (pin_release_chunk),
        .any_pending    ()
    );

    // The fabric manager's reclaim probe reads the authoritative entry: an
    // update may commit only at zero pin count with no pending reclaim.
    assign reclaim_allowed = (dir_adv_pins == '0) & ~dir_adv_pend;

    // OAT advisory verdict: a descriptor proceeds past S2 into scoring on the
    // PCM pass alone. The final commit is the directory CAS at S7.
    assign oat_pass   = pcm_pass;
    assign pin_reject = pcm_pass & ~pin_alloc_ok;   // advisory resource early-out
    assign stat_pin_count = 10'(pin_count_w);

    // RELEASE wiring is declared here, driven from the S8 completion logic below.
    logic        pin_release_valid;
    logic [8:0]  pin_release_chunk;
    logic [15:0] pin_release_gen;

    //=========================================================================
    // S3: Expert Bank Read (1 cycle, 7 parallel banks)
    // Per-bank clock gating: each bank has its own ICG. A bank is clocked only
    // when it is in the active expert mask OR is the target of a feedback write
    // this cycle. In Core2 (active mask = 2 experts, e.g. Persistence+Momentum)
    // the other 5 banks are gated to retention and burn only leakage.
    //=========================================================================
    logic [8:0] s3_addr;
    logic       s3_rd_en;

    assign s3_addr  = pcm_chunk_id;
    // Only an OAT pass (PCM validated AND pin acquired) enters scoring. A
    // validated descriptor that could not acquire a pin (pin_reject) is treated
    // exactly like a PCM reject: it never enters S3-S6 and issues no payload.
    assign s3_rd_en = oat_pass;

    // Feedback write is gated by pipeline_idle so a step-t attention write
    // cannot become visible to a step-t scoring read (t-1 causal boundary).
    logic fb_wr_en_gated;
    assign fb_wr_en_gated = fb_valid & pipeline_idle;

    logic [NUM_EXPERTS-1:0] bank_clk;
    logic [NUM_EXPERTS-1:0] bank_clk_en;

    genvar g;
    generate
        for (g = 0; g < NUM_EXPERTS; g++) begin : gen_expert_banks
            // Bank is live if active in the current core config, or if it is
            // the destination of this cycle's (idle-gated) feedback write.
            assign bank_clk_en[g] = clk_en &
                (cfg_expert_active_mask[g]
                 | (fb_wr_en_gated & (fb_expert_id == g[2:0])));

            ICG icg_bank (
                .CK  (clk),
                .E   (bank_clk_en[g]),
                .SE  (1'b0),
                .GCK (bank_clk[g])
            );

            APEX_EXPERT_BANK u_bank (
                .clk     (bank_clk[g]),
                .rst_n   (rst_n),
                .rd_addr (s3_addr),
                .rd_en   (s3_rd_en),
                .rd_data (expert_pred[g]),
                .wr_addr (fb_chunk_id),
                .wr_data (fb_attention_mass),
                .wr_en   (fb_wr_en_gated && (fb_expert_id == g[2:0]))
            );
        end
    endgenerate

    // S3 output register (expert read has 1-cycle latency built into bank)
    logic [15:0] s3_epoch;
    always_ff @(posedge s34_clk or negedge rst_n) begin
        if (!rst_n) begin
            expert_rd_valid <= 1'b0;
        end else if (!pipe_stall) begin
            expert_rd_valid <= oat_pass & pcm_valid;
            s3_chunk_id     <= pcm_chunk_id;
            s3_epoch        <= pcm_epoch;
        end
    end

    //=========================================================================
    // S4: MAC Accumulation (1 cycle)
    //=========================================================================
    APEX_MAC_ARRAY u_mac (
        .clk          (s34_clk),
        .rst_n        (rst_n),
        .stall        (pipe_stall),
        .pred_0       (expert_pred[0]),
        .pred_1       (expert_pred[1]),
        .pred_2       (expert_pred[2]),
        .pred_3       (expert_pred[3]),
        .pred_4       (expert_pred[4]),
        .pred_5       (expert_pred[5]),
        .pred_6       (expert_pred[6]),
        .pred_valid   (expert_rd_valid),
        .weight_0     (expert_weights[0]),
        .weight_1     (expert_weights[1]),
        .weight_2     (expert_weights[2]),
        .weight_3     (expert_weights[3]),
        .weight_4     (expert_weights[4]),
        .weight_5     (expert_weights[5]),
        .weight_6     (expert_weights[6]),
        .chunk_id_in  (s3_chunk_id),
        .epoch_in     (s3_epoch),
        .score_out    (mac_score),
        .chunk_id_out (mac_chunk_id),
        .epoch_out    (mac_epoch),
        .score_valid  (mac_valid)
    );

    //=========================================================================
    // S5: Dual-Zone Exact Top-K (2 cycles)
    // EZ: 7-entry min-heap (depth 2), root = global minimum
    // SZ: 18-entry flat array, safe_min via 17-comparator min-tree
    // Three-branch admission: reject / EZ-local / cross-zone
    // Case 2 asserts case2_stall for one cycle -> pipe_stall freezes the front
    // so the next descriptor is classified only after safe_min is refreshed.
    // The speculative forwarding tree makes that single-cycle stall sufficient.
    //=========================================================================
    logic [4:0] heap_count;

    APEX_TOPK_HEAP #(.K(25)) u_heap (
        .clk              (gated_clk),
        .rst_n            (rst_n),
        .new_score        (mac_score),
        .new_chunk_id     (mac_chunk_id),
        .new_valid        (mac_valid),
        .hold             (pipe_stall),
        .admitted         (heap_admitted),
        .admitted_valid   (heap_admitted_valid),
        .case2_stall      (case2_stall),
        .readout_start    (1'b0),  // Readout controlled externally
        .readout_chunk_id (),
        .readout_score    (),
        .readout_valid    (),
        .readout_done     (),
        .flush            (cfg_flush_gated),  // Use gated flush!
        .heap_count       (heap_count),
        .heap_idle        (heap_idle)
    );

    //=========================================================================
    // S6: Weight Update (overlapped, uses previous step's feedback)
    // Triggered by cfg_flush_gated (drain protocol ensures safety)
    //=========================================================================
    logic [2:0] loss_q [0:NUM_EXPERTS-1];
    logic        loss_valid;

    // Loss computation: compares admitted expert predictions against actual
    // attention mass feedback from GPU. Produces per-expert quantized loss.
    APEX_LOSS_COMPUTE #(
        .NUM_EXPERTS (NUM_EXPERTS),
        .SCORE_W     (16),
        .ID_W        (9)
    ) u_loss (
        .clk                (gated_clk),
        .rst_n              (rst_n),
        .admit_valid        (heap_admitted_valid & heap_admitted & ~pipe_stall),
        .admit_chunk_id     (mac_chunk_id),
        .admit_expert_preds_flat ({expert_pred[6], expert_pred[5], expert_pred[4],
                                   expert_pred[3], expert_pred[2], expert_pred[1],
                                   expert_pred[0]}),
        .fb_valid           (fb_valid),
        .fb_chunk_id        (fb_chunk_id),
        .fb_attention_mass  (fb_attention_mass),
        .loss_q_flat        ({loss_q[6], loss_q[5], loss_q[4],
                              loss_q[3], loss_q[2], loss_q[1], loss_q[0]}),
        .loss_valid         (loss_valid)
    );

    APEX_WEIGHT_UPDATE #(.NUM_EXPERTS(NUM_EXPERTS)) u_weights (
        .clk                (gated_clk),
        .rst_n              (rst_n),
        .loss_q             (loss_q),
        .update_trigger     (cfg_flush_gated),  // Gated: only fires when drained
        .eta_q              (cfg_eta_q),
        .expert_active_mask (cfg_expert_active_mask),
        .weights            (expert_weights)
    );

    //=========================================================================
    // Stochastic Exploration (SEA): injects probe descriptors when coverage
    // is low. Decays to zero exploration when coverage exceeds threshold.
    //=========================================================================
    logic        sea_probe;
    logic [8:0]  sea_probe_chunk_id;
    logic [7:0]  sea_epsilon;
    logic [15:0] sea_coverage;

    APEX_SEA u_sea (
        .clk            (gated_clk),
        .rst_n          (rst_n),
        .desc_valid     (s1_valid),
        .desc_chunk_id  (s1_chunk_id),
        .step_boundary  (cfg_flush_gated),
        .pipe_idle      (~cmd_valid & ~pipe_stall),
        .enable         (cfg_sea_enable),
        .probe_inject   (sea_probe),
        .probe_chunk_id (sea_probe_chunk_id),
        .sea_epsilon    (sea_epsilon),
        .sea_coverage   (sea_coverage)
    );

    //=========================================================================
    // Pipeline Controller
    //=========================================================================
    logic drain_stall;
    APEX_PIPELINE_CTRL u_ctrl (
        .clk             (gated_clk),
        .rst_n           (rst_n),
        .s1_valid        (s1_valid),
        .s2_valid        (pcm_valid),
        // A pin-less validated descriptor rejects on the same 4-cycle bypass as
        // a PCM reject, so resource exhaustion never issues payload.
        .s2_reject       (pcm_reject | pin_reject),
        .s3_valid        (expert_rd_valid),
        .s4_valid        (mac_valid),
        .s5_valid        (heap_admitted_valid),
        .s6_valid        (1'b0),  // Overlapped, not on critical path
        .dma_ready       (dma_ready),
        .cpl_ready       (cpl_ready),
        .case2_stall     (case2_stall),
        .pipe_stall      (pipe_stall),
        .drain_stall     (drain_stall),
        .reject_bypass   (reject_bypass),
        .s7_issue_dma    (s7_issue_dma),
        .s7_null_cpl     (s7_null_cpl),
        .bypass_cpl_valid(bypass_cpl_valid),
        .bypass_chunk_id (bypass_chunk_id),
        .pcm_chunk_id    (pcm_chunk_id)
    );

    //=========================================================================
    // S7: DMA Issue + admit-completion staging (1 cycle)
    //=========================================================================

    // DMA output (admitted path). The payload gate opens only on a successful
    // directory CAS at the commit stage; a failed CAS never issues a beat.
    // Gated by drain_stall (backpressure) only, not by case2_stall.
    always_ff @(posedge gated_clk or negedge rst_n) begin
        if (!rst_n) begin
            dma_valid <= 1'b0;
        end else begin
            dma_valid    <= heap_admitted_valid & heap_admitted & dir_cas_ok & ~drain_stall;
            dma_chunk_id <= mac_chunk_id;
            dma_score    <= mac_score;
        end
    end

    // S7 admit-completion stage register. The admitted decision from S5b is
    // registered here (S7), then registered again at S8 below — giving the
    // admit path its 9th cycle. Rejects do NOT flow through here; they take the
    // direct bypass into S8 and stay at 4 cycles.
    logic       s7_cpl_valid;
    logic [8:0] s7_cpl_chunk_id;
    logic [1:0] s7_cpl_status;

    always_ff @(posedge gated_clk or negedge rst_n) begin
        if (!rst_n) begin
            s7_cpl_valid  <= 1'b0;
            s7_cpl_status <= 2'b00;
        end else begin
            s7_cpl_valid    <= heap_admitted_valid & ~drain_stall;
            s7_cpl_chunk_id <= mac_chunk_id;
            s7_cpl_status   <= (heap_admitted & dir_cas_ok) ? 2'b01 : 2'b10;
        end
    end

    //=========================================================================
    // S8: MMIO Completion Register (admit-only 9th stage)
    //   - Reject/bypass path: written directly from the S2b bypass -> 4 cycles.
    //   - Admit path: written from the S7 stage register -> 9 cycles total
    //     (S1 + S2a + S2b + S3 + S4 + S5a + S5b + S7 + S8).
    //   Bypass has priority; a reject and an admit cannot target the same
    //   completion slot in the same cycle because the front is frozen during
    //   the Case 2 stall and rejects are single-cycle bypass events.
    //=========================================================================
    always_ff @(posedge gated_clk or negedge rst_n) begin
        if (!rst_n) begin
            cpl_valid    <= 1'b0;
            cpl_chunk_id <= '0;
            cpl_status   <= 2'b00;
        end else if (bypass_cpl_valid) begin
            cpl_valid    <= 1'b1;
            cpl_chunk_id <= bypass_chunk_id;
            cpl_status   <= 2'b10;  // REJECTED (4-cycle path)
        end else if (s7_cpl_valid) begin
            cpl_valid    <= 1'b1;
            cpl_chunk_id <= s7_cpl_chunk_id;
            cpl_status   <= s7_cpl_status;  // ADMITTED/rejected-by-heap (9-cycle path)
        end else begin
            cpl_valid    <= 1'b0;
        end
    end

    //=========================================================================
    // RELEASE(d) — one cycle after the directory CAS installs the pin.
    //
    // The CAS at the commit stage (S7) installs the pin, and the release fires
    // on the following edge, so allocation and release stay 1:1 by
    // construction and independent of downstream backpressure. This models the
    // evaluated short-transfer case; a DMA-drain-scoped release for long
    // transfers is the cefe_dma_engine extension (see that module and
    // LIMITATIONS). The release drives both the in-flight index and the
    // authoritative directory entry, and it fires only on a CAS success, so a
    // rejected descriptor never frees another transfer's pin.
    //=========================================================================
    always_ff @(posedge gated_clk or negedge rst_n) begin
        if (!rst_n) begin
            pin_release_valid <= 1'b0;
            pin_release_chunk <= '0;
            pin_release_gen   <= '0;
        end else begin
            pin_release_valid <= dir_cas_ok & ~pipe_stall;
            pin_release_chunk <= mac_chunk_id;
            pin_release_gen   <= mac_epoch;
        end
    end

    //=========================================================================
    // Statistics
    //=========================================================================
    always_ff @(posedge gated_clk or negedge rst_n) begin
        if (!rst_n) begin
            stat_admitted    <= '0;
            stat_rejected    <= '0;
            stat_total_cycles <= '0;
            stat_pin_reject  <= 1'b0;
        end else begin
            stat_total_cycles <= stat_total_cycles + 1'b1;
            stat_pin_reject   <= pin_reject;
            if (cpl_valid && cpl_status == 2'b01)
                stat_admitted <= stat_admitted + 1'b1;
            if (cpl_valid && cpl_status == 2'b10)
                stat_rejected <= stat_rejected + 1'b1;
        end
    end

endmodule
