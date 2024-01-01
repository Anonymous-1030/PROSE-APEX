//=============================================================================
// CEFE VC-WRR Arbiter — 16-Host Virtual Channel Weighted Round-Robin
//
// Implements a Deficit Round-Robin (DRR) arbiter over 16 per-host queues.
// Properties:
//   - Per-VC quantum added once per round (when rr_ptr returns to 0).
//   - Grant is made to the first eligible VC in the round-robin scan.
//   - Anti-starvation: every enabled non-empty VC receives at least its weight
//     quantum each round, guaranteeing a minimum bandwidth fraction of
//     weight[i] / sum(weights).
//   - All state freezes when pipe_stall is asserted; output valid is held.
//
// Target: ASAP7 7nm @ 1 GHz
//=============================================================================
`timescale 1ns/1ps

module cefe_vc_wrr #(
    parameter int NUM_VC       = 16,
    parameter int QUEUE_DEPTH  = 32,
    parameter int DESC_WIDTH   = 128,
    parameter int WEIGHT_BITS  = 4,
    parameter int PTR_BITS     = $clog2(QUEUE_DEPTH),
    parameter int VC_BITS      = $clog2(NUM_VC)
)(
    input  logic                    clk,
    input  logic                    rst_n,

    // Push Interface (16 hosts)
    input  logic [NUM_VC-1:0]       push_valid,
    output logic [NUM_VC-1:0]       push_ready,
    input  logic [DESC_WIDTH-1:0]   push_data  [0:NUM_VC-1],

    // Pop Interface (to downstream APEX pipeline)
    output logic                    pop_valid,
    input  logic                    pop_ready,
    output logic [DESC_WIDTH-1:0]   pop_data,
    output logic [VC_BITS-1:0]      pop_vc_id,

    // Backpressure from downstream
    input  logic                    pipe_stall,

    // Configuration (quasi-static, set at init)
    input  logic [WEIGHT_BITS-1:0]  cfg_weight   [0:NUM_VC-1],
    input  logic [NUM_VC-1:0]       cfg_vc_enable
);

    //=========================================================================
    // Per-VC Queue Storage
    //=========================================================================
    logic [DESC_WIDTH-1:0] queue_mem [0:NUM_VC-1][0:QUEUE_DEPTH-1];
    logic [PTR_BITS-1:0]   wr_ptr   [0:NUM_VC-1];
    logic [PTR_BITS-1:0]   rd_ptr   [0:NUM_VC-1];
    logic [PTR_BITS:0]     count    [0:NUM_VC-1];

    logic [NUM_VC-1:0] vc_empty;
    logic [NUM_VC-1:0] vc_full;

    genvar gi;
    generate
        for (gi = 0; gi < NUM_VC; gi++) begin : gen_vc_status
            assign vc_empty[gi] = (count[gi] == '0);
            assign vc_full[gi]  = (count[gi] == QUEUE_DEPTH[PTR_BITS:0]);
            assign push_ready[gi] = cfg_vc_enable[gi] & ~vc_full[gi];
        end
    endgenerate

    //=========================================================================
    // Queue Write Logic
    //=========================================================================
    generate
        for (gi = 0; gi < NUM_VC; gi++) begin : gen_queue_write
            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) begin
                    wr_ptr[gi] <= '0;
                end else if (push_valid[gi] & push_ready[gi]) begin
                    queue_mem[gi][wr_ptr[gi]] <= push_data[gi];
                    wr_ptr[gi] <= (wr_ptr[gi] == PTR_BITS'(QUEUE_DEPTH - 1))
                                  ? '0 : wr_ptr[gi] + 1'b1;
                end
            end
        end
    endgenerate

    //=========================================================================
    // Deficit Round-Robin Arbiter
    //=========================================================================
    logic [WEIGHT_BITS:0]  deficit    [0:NUM_VC-1];
    logic [VC_BITS-1:0]    rr_ptr;
    logic                  round_start;
    logic                  grant_valid;
    logic [VC_BITS-1:0]    grant_vc;

    // Deficit saturation cap. Without an upper bound, a VC that is enabled but
    // repeatedly cannot be granted (or a malicious tenant that games the round
    // boundary) would accumulate deficit until the WEIGHT_BITS+1 counter wraps
    // (e.g. 31+15 -> 14 mod 32), collapsing its priority OR letting it hoard an
    // unbounded quantum. Clamping at all-ones bounds any single VC to at most
    // MAX_DEFICIT credits, so it can never starve the others by hoarding.
    localparam logic [WEIGHT_BITS:0] MAX_DEFICIT = {(WEIGHT_BITS+1){1'b1}};

    // Output register
    logic                  pop_valid_r;
    logic [DESC_WIDTH-1:0] pop_data_r;
    logic [VC_BITS-1:0]    pop_vc_id_r;

    assign round_start = (rr_ptr == '0);

    // Combinational grant: scan from rr_ptr, first eligible VC
    logic [VC_BITS-1:0] selected_vc;
    logic               selected_valid;

    always_comb begin
        selected_vc    = rr_ptr;
        selected_valid = 1'b0;
        for (int i = 0; i < NUM_VC; i++) begin
            logic [VC_BITS-1:0] idx;
            idx = rr_ptr + VC_BITS'(i);
            if (!selected_valid &&
                cfg_vc_enable[idx] &&
                !vc_empty[idx] &&
                (deficit[idx] > '0)) begin
                selected_vc    = idx;
                selected_valid = 1'b1;
            end
        end
    end

    assign grant_valid = selected_valid;
    assign grant_vc    = selected_vc;

    // Queue read pointer + count management
    generate
        for (gi = 0; gi < NUM_VC; gi++) begin : gen_queue_count
            wire this_push = push_valid[gi] & push_ready[gi];
            wire this_pop  = grant_valid && (grant_vc == VC_BITS'(gi)) && pop_ready && ~pipe_stall;

            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) begin
                    rd_ptr[gi] <= '0;
                    count[gi]  <= '0;
                end else begin
                    case ({this_push, this_pop})
                        2'b10: count[gi] <= count[gi] + 1'b1;
                        2'b01: begin
                            count[gi]  <= count[gi] - 1'b1;
                            rd_ptr[gi] <= (rd_ptr[gi] == PTR_BITS'(QUEUE_DEPTH - 1))
                                          ? '0 : rd_ptr[gi] + 1'b1;
                        end
                        2'b11: begin
                            rd_ptr[gi] <= (rd_ptr[gi] == PTR_BITS'(QUEUE_DEPTH - 1))
                                          ? '0 : rd_ptr[gi] + 1'b1;
                        end
                        default: ;
                    endcase
                end
            end
        end
    endgenerate

    // Sequential: deficit counters, rr_ptr, output register
    logic [DESC_WIDTH-1:0] pop_data_comb;

    always_comb begin
        pop_data_comb = '0;
        for (int i = 0; i < NUM_VC; i++) begin
            if (grant_vc == VC_BITS'(i))
                pop_data_comb = queue_mem[i][rd_ptr[i]];
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rr_ptr      <= '0;
            pop_valid_r <= 1'b0;
            pop_data_r  <= '0;
            pop_vc_id_r <= '0;
            for (int i = 0; i < NUM_VC; i++)
                deficit[i] <= '0;
        end else if (!pipe_stall) begin
            // Replenish quanta at round boundary for all enabled non-empty VCs
            if (round_start) begin
                for (int i = 0; i < NUM_VC; i++) begin
                    if (cfg_vc_enable[i] && !vc_empty[i]) begin
                        logic [WEIGHT_BITS+1:0] add;  // one extra bit for carry
                        add = {1'b0, deficit[i]} + {2'b0, cfg_weight[i]};
                        // Saturate at MAX_DEFICIT to prevent wrap-around and
                        // unbounded credit hoarding by a flooding tenant.
                        deficit[i] <= (add > {1'b0, MAX_DEFICIT})
                                      ? MAX_DEFICIT
                                      : add[WEIGHT_BITS:0];
                    end else begin
                        deficit[i] <= '0;
                    end
                end
            end

            // Decrement deficit for granted VC
            if (grant_valid && pop_ready) begin
                deficit[grant_vc] <= deficit[grant_vc] - 1'b1;
            end

            // Advance round-robin pointer every non-stall cycle
            rr_ptr <= rr_ptr + 1'b1;

            // Output register
            pop_valid_r <= grant_valid;
            pop_data_r  <= pop_data_comb;
            pop_vc_id_r <= grant_vc;
        end
        // else: hold all state and output valid during stall
    end

    assign pop_valid = pop_valid_r;
    assign pop_data  = pop_data_r;
    assign pop_vc_id = pop_vc_id_r;

endmodule
