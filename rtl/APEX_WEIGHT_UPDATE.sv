//=============================================================================
// APEX Weight Update Engine (Hedge Algorithm) — Pipelined Division Version
//
// Implements multiplicative weight update: w_k <- w_k * exp(-eta * loss_k)
//   - 64-entry LUT for exp(-eta*loss) indexed by {eta_q[2:0], loss_q[2:0]}
//   - 7 parallel 8b*8b multipliers for weight scaling
//   - Pipelined bit-serial divider (24 cycles) for normalization
//   - Remainder assigned to highest-weight active expert so sum(weights) = 255
//
// Operation:
//   - update_trigger (one-cycle pulse) starts the update FSM.
//   - During division the old weights remain on the output port.
//   - After ~DIV_CYCLES the weight registers are updated.
//
// This off-critical-path unit runs once per decode step, so the multi-cycle
// latency is fully hidden by the inter-step boundary.
//
// Target: ASAP7 7nm @ 1GHz
//=============================================================================
`timescale 1ns/1ps

import apex_pkg::*;

module APEX_WEIGHT_UPDATE #(
    parameter int NUM_EXPERTS = apex_pkg::NUM_EXPERTS,
    parameter int DIV_CYCLES  = 24   // Bits of precision for division
)(
    input  logic        clk,
    input  logic        rst_n,

    input  logic [2:0]  loss_q [0:NUM_EXPERTS-1],
    input  logic        update_trigger,

    input  logic [2:0]  eta_q,
    input  logic [NUM_EXPERTS-1:0] expert_active_mask,

    output logic [NUM_EXPERTS-1:0][7:0] weights
);

    //=========================================================================
    // exp(-eta*loss) LUT
    //=========================================================================
    function automatic [7:0] exp_lut_fn(input [5:0] addr);
        case (addr)
            6'd0:  exp_lut_fn = 8'd255;  6'd1:  exp_lut_fn = 8'd253;
            6'd2:  exp_lut_fn = 8'd251;  6'd3:  exp_lut_fn = 8'd249;
            6'd4:  exp_lut_fn = 8'd248;  6'd5:  exp_lut_fn = 8'd246;
            6'd6:  exp_lut_fn = 8'd244;  6'd7:  exp_lut_fn = 8'd242;
            6'd8:  exp_lut_fn = 8'd255;  6'd9:  exp_lut_fn = 8'd251;
            6'd10: exp_lut_fn = 8'd248;  6'd11: exp_lut_fn = 8'd244;
            6'd12: exp_lut_fn = 8'd240;  6'd13: exp_lut_fn = 8'd237;
            6'd14: exp_lut_fn = 8'd233;  6'd15: exp_lut_fn = 8'd230;
            6'd16: exp_lut_fn = 8'd255;  6'd17: exp_lut_fn = 8'd249;
            6'd18: exp_lut_fn = 8'd244;  6'd19: exp_lut_fn = 8'd238;
            6'd20: exp_lut_fn = 8'd233;  6'd21: exp_lut_fn = 8'd228;
            6'd22: exp_lut_fn = 8'd222;  6'd23: exp_lut_fn = 8'd217;
            6'd24: exp_lut_fn = 8'd255;  6'd25: exp_lut_fn = 8'd248;
            6'd26: exp_lut_fn = 8'd240;  6'd27: exp_lut_fn = 8'd233;
            6'd28: exp_lut_fn = 8'd226;  6'd29: exp_lut_fn = 8'd219;
            6'd30: exp_lut_fn = 8'd212;  6'd31: exp_lut_fn = 8'd206;
            6'd32: exp_lut_fn = 8'd255;  6'd33: exp_lut_fn = 8'd244;
            6'd34: exp_lut_fn = 8'd233;  6'd35: exp_lut_fn = 8'd222;
            6'd36: exp_lut_fn = 8'd212;  6'd37: exp_lut_fn = 8'd203;
            6'd38: exp_lut_fn = 8'd194;  6'd39: exp_lut_fn = 8'd185;
            6'd40: exp_lut_fn = 8'd255;  6'd41: exp_lut_fn = 8'd240;
            6'd42: exp_lut_fn = 8'd226;  6'd43: exp_lut_fn = 8'd212;
            6'd44: exp_lut_fn = 8'd200;  6'd45: exp_lut_fn = 8'd188;
            6'd46: exp_lut_fn = 8'd177;  6'd47: exp_lut_fn = 8'd166;
            6'd48: exp_lut_fn = 8'd255;  6'd49: exp_lut_fn = 8'd237;
            6'd50: exp_lut_fn = 8'd219;  6'd51: exp_lut_fn = 8'd203;
            6'd52: exp_lut_fn = 8'd188;  6'd53: exp_lut_fn = 8'd174;
            6'd54: exp_lut_fn = 8'd161;  6'd55: exp_lut_fn = 8'd149;
            6'd56: exp_lut_fn = 8'd255;  6'd57: exp_lut_fn = 8'd233;
            6'd58: exp_lut_fn = 8'd212;  6'd59: exp_lut_fn = 8'd194;
            6'd60: exp_lut_fn = 8'd177;  6'd61: exp_lut_fn = 8'd161;
            6'd62: exp_lut_fn = 8'd147;  6'd63: exp_lut_fn = 8'd134;
            default: exp_lut_fn = 8'd255;
        endcase
    endfunction

    //=========================================================================
    // FSM
    //=========================================================================
    localparam [1:0] ST_IDLE   = 2'd0;
    localparam [1:0] ST_DIV    = 2'd1;
    localparam [1:0] ST_FINISH = 2'd2;

    reg [1:0] state;
    reg [$clog2(DIV_CYCLES+1)-1:0] div_cnt;

    //=========================================================================
    // Latched inputs and working registers
    //=========================================================================
    reg [7:0]  old_weights     [0:NUM_EXPERTS-1];
    reg [7:0]  exp_factor_r    [0:NUM_EXPERTS-1];
    reg [15:0] raw_weight_r    [0:NUM_EXPERTS-1];
    reg [23:0] dividend_r      [0:NUM_EXPERTS-1];
    reg [17:0] weight_sum_r;
    reg [6:0]  active_mask_r;

    reg [DIV_CYCLES-1:0] quot      [0:NUM_EXPERTS-1];
    reg [17:0]           remainder [0:NUM_EXPERTS-1];

    //=========================================================================
    // Combinational pre-computation
    //=========================================================================
    wire [7:0]  exp_factor_comb [0:NUM_EXPERTS-1];
    wire [15:0] raw_weight_comb [0:NUM_EXPERTS-1];

    genvar gi;
    generate
        for (gi = 0; gi < NUM_EXPERTS; gi++) begin : gen_pre
            assign exp_factor_comb[gi] = exp_lut_fn({eta_q, loss_q[gi]});
            assign raw_weight_comb[gi] = expert_active_mask[gi] ?
                                         ({8'b0, weights[gi]} * {8'b0, exp_factor_comb[gi]}) : 16'd0;
        end
    endgenerate

    wire [17:0] weight_sum_comb = {2'b0, raw_weight_comb[0]}
                                + {2'b0, raw_weight_comb[1]}
                                + {2'b0, raw_weight_comb[2]}
                                + {2'b0, raw_weight_comb[3]}
                                + {2'b0, raw_weight_comb[4]}
                                + {2'b0, raw_weight_comb[5]}
                                + {2'b0, raw_weight_comb[6]};

    //=========================================================================
    // Normalization helper variables (registered to avoid always-block locals)
    //=========================================================================
    reg [7:0]  norm_w     [0:NUM_EXPERTS-1];
    reg [9:0]  norm_sum_r;
    reg [7:0]  remainder_val_r;
    reg [2:0]  max_idx_r;
    reg [7:0]  max_val_r;

    //=========================================================================
    // Sequential update FSM
    //=========================================================================
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= ST_IDLE;
            div_cnt <= '0;
            weight_sum_r <= '0;
            active_mask_r <= '0;
            norm_sum_r <= '0;
            remainder_val_r <= '0;
            max_idx_r <= '0;
            max_val_r <= '0;
            for (i = 0; i < NUM_EXPERTS; i = i + 1) begin
                weights[i]       <= 8'd36;
                old_weights[i]   <= 8'd36;
                exp_factor_r[i]  <= 8'd255;
                raw_weight_r[i]  <= 16'd0;
                dividend_r[i]    <= '0;
                remainder[i]     <= '0;
                quot[i]          <= '0;
                norm_w[i]        <= '0;
            end
        end else begin
            case (state)
                ST_IDLE: begin
                    if (update_trigger) begin
                        state <= ST_DIV;
                        div_cnt <= DIV_CYCLES[$clog2(DIV_CYCLES+1)-1:0];
                        active_mask_r <= expert_active_mask;
                        weight_sum_r  <= weight_sum_comb;
                        for (i = 0; i < NUM_EXPERTS; i = i + 1) begin
                            old_weights[i]  <= weights[i];
                            exp_factor_r[i] <= exp_factor_comb[i];
                            raw_weight_r[i] <= raw_weight_comb[i];
                            // dividend = raw_weight * 255 (fits in 24 bits)
                            dividend_r[i]   <= raw_weight_comb[i] * 24'd255;
                            remainder[i]    <= '0;
                            quot[i]         <= '0;
                        end
                    end
                end

                ST_DIV: begin
                    for (i = 0; i < NUM_EXPERTS; i = i + 1) begin
                        logic [17:0] rem_shift;
                        logic [18:0] rem_minus_div;
                        rem_shift = {remainder[i][16:0], dividend_r[i][DIV_CYCLES-1]};
                        rem_minus_div = {1'b0, rem_shift} - {1'b0, weight_sum_r};
                        if (rem_minus_div[18] == 1'b0) begin
                            // rem_shift >= weight_sum
                            remainder[i] <= rem_minus_div[17:0];
                            quot[i]      <= {quot[i][DIV_CYCLES-2:0], 1'b1};
                        end else begin
                            remainder[i] <= rem_shift;
                            quot[i]      <= {quot[i][DIV_CYCLES-2:0], 1'b0};
                        end
                        dividend_r[i] <= {dividend_r[i][DIV_CYCLES-2:0], 1'b0};
                    end

                    if (div_cnt == 1)
                        state <= ST_FINISH;
                    else
                        div_cnt <= div_cnt - 1'b1;
                end

                ST_FINISH: begin
                    // Compute normalized weights and distribute remainder
                    norm_sum_r = '0;
                    max_idx_r  = 3'd0;
                    max_val_r  = 8'd0;

                    for (i = 0; i < NUM_EXPERTS; i = i + 1) begin
                        if (!active_mask_r[i]) begin
                            norm_w[i] = 8'd0;
                        end else begin
                            norm_w[i] = (quot[i] > 8'd255) ? 8'd255 : quot[i][7:0];
                            if (norm_w[i] == 8'd0)
                                norm_w[i] = 8'd1;  // floor clamp
                        end
                        norm_sum_r = norm_sum_r + {2'b0, norm_w[i]};
                        if (norm_w[i] > max_val_r) begin
                            max_val_r = norm_w[i];
                            max_idx_r = i[2:0];
                        end
                    end

                    remainder_val_r = (norm_sum_r < 10'd255) ? (8'd255 - norm_sum_r[7:0]) : 8'd0;

                    for (i = 0; i < NUM_EXPERTS; i = i + 1) begin
                        if (!active_mask_r[i])
                            weights[i] <= 8'd0;
                        else if (i[2:0] == max_idx_r)
                            weights[i] <= norm_w[i] + remainder_val_r;
                        else
                            weights[i] <= norm_w[i];
                    end

                    state <= ST_IDLE;
                end

                default: state <= ST_IDLE;
            endcase
        end
    end

endmodule
