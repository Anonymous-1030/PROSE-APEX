//=============================================================================
// Testbench for cefe_directory — consult-then-CAS atomicity (paper Section
// IV-B and the reviewer's interleaving).
//
//   T1 (directed, the reviewer's sequence): an advisory read happens, then a
//      placement update commits on the same entry, then the CAS stage tries to
//      install the pin. Expected: the transaction rejects and cas_ok stays
//      low, so no payload can issue.
//   T2 (update after the linearization point): the CAS succeeds first, the
//      update diverts to pending_reclaim, new pins are rejected while pending,
//      and the drain commits the waiting update on the last release.
//   T3 (adversary): randomized placement updates injected inside the OAT
//      window with random delays, across randomized descriptor generations.
//      A golden consult-then-CAS model replays the same stimulus; the RTL
//      verdict stream must match it exactly, and no successful CAS may ever
//      bind a descriptor to a stale generation (stale payload count = 0).
//
// Run:  iverilog -g2012 -o cefe_directory_tb_sim \
//          cefe_directory.sv cefe_directory_tb.sv && vvp cefe_directory_tb_sim
//=============================================================================
`timescale 1ns/1ps

module cefe_directory_tb;

    localparam int NUM_CHUNKS = 512;
    localparam int GEN_W      = 16;
    localparam int PIN_W      = 3;

    logic clk = 0, rst_n = 0;
    always #0.5 clk = ~clk;   // 1 GHz

    logic [8:0]          adv_chunk;
    logic [GEN_W-1:0]    adv_gen;
    logic                adv_resident;
    logic                adv_pending;
    logic [PIN_W-1:0]    adv_pin_count;

    logic                cas_valid;
    logic [8:0]          cas_chunk;
    logic [GEN_W-1:0]    cas_gen;
    logic                cas_pin_free;
    logic                cas_ok;

    logic                upd_valid;
    logic [8:0]          upd_chunk;
    logic [GEN_W-1:0]    upd_gen;
    logic                upd_resident;
    logic                upd_committed;
    logic                upd_pended;

    logic                rel_valid;
    logic [8:0]          rel_chunk;
    logic                any_pending;

    int errors = 0;

    cefe_directory #(.NUM_CHUNKS(NUM_CHUNKS), .GEN_W(GEN_W), .PIN_W(PIN_W)) dut (
        .clk(clk), .rst_n(rst_n),
        .adv_chunk(adv_chunk), .adv_gen(adv_gen),
        .adv_resident(adv_resident), .adv_pending(adv_pending),
        .adv_pin_count(adv_pin_count),
        .cas_valid(cas_valid), .cas_chunk(cas_chunk), .cas_gen(cas_gen),
        .cas_pin_free(cas_pin_free), .cas_ok(cas_ok),
        .upd_valid(upd_valid), .upd_chunk(upd_chunk), .upd_gen(upd_gen),
        .upd_resident(upd_resident),
        .upd_committed(upd_committed), .upd_pended(upd_pended),
        .rel_valid(rel_valid), .rel_chunk(rel_chunk),
        .any_pending(any_pending)
    );

    task automatic check(input logic cond, input string msg);
        if (!cond) begin
            $display("  [FAIL] %s", msg);
            errors = errors + 1;
        end else begin
            $display("  [ok]   %s", msg);
        end
    endtask

    task automatic idle_inputs;
        cas_valid = 0; upd_valid = 0; rel_valid = 0;
        cas_pin_free = 1;
    endtask

    task automatic place(input [8:0] c, input [GEN_W-1:0] g, input logic res);
        @(negedge clk);
        upd_valid = 1; upd_chunk = c; upd_gen = g; upd_resident = res;
        @(negedge clk);
        upd_valid = 0;
    endtask

    task automatic do_cas(input [8:0] c, input [GEN_W-1:0] g, output logic ok);
        @(negedge clk);
        cas_valid = 1; cas_chunk = c; cas_gen = g;
        #0.1 ok = cas_ok;          // sample the combinational CAS verdict
        @(negedge clk);
        cas_valid = 0;
    endtask

    task automatic do_release(input [8:0] c);
        @(negedge clk);
        rel_valid = 1; rel_chunk = c;
        @(negedge clk);
        rel_valid = 0;
    endtask

    //=========================================================================
    // Golden consult-then-CAS model (behavioral, cycle aligned with the TB
    // drive points). Verdict rule: a CAS succeeds iff the entry generation and
    // resident state at the CAS cycle match and no reclaim is pending; updates
    // commit only at zero pins and otherwise pend; the drain commits the
    // pended update on the last release.
    //=========================================================================
    logic [GEN_W-1:0] m_gen      [0:NUM_CHUNKS-1];
    logic             m_resident [0:NUM_CHUNKS-1];
    int               m_pins     [0:NUM_CHUNKS-1];
    logic             m_pend     [0:NUM_CHUNKS-1];
    logic [GEN_W-1:0] m_pgen     [0:NUM_CHUNKS-1];
    logic             m_pres     [0:NUM_CHUNKS-1];

    task automatic m_reset;
        for (int k = 0; k < NUM_CHUNKS; k++) begin
            m_gen[k] = '0; m_resident[k] = 0; m_pins[k] = 0; m_pend[k] = 0;
        end
    endtask

    function automatic logic m_cas(input [8:0] c, input [GEN_W-1:0] g,
                                   input logic pin_free);
        if ((m_gen[c] == g) && m_resident[c] && !m_pend[c] && pin_free) begin
            m_pins[c] = m_pins[c] + 1;
            return 1'b1;
        end
        return 1'b0;
    endfunction

    function automatic logic m_update(input [8:0] c, input [GEN_W-1:0] g,
                                      input logic res, input logic cas_won);
        if ((m_pins[c] == 0) && !cas_won) begin
            m_gen[c] = g; m_resident[c] = res;
            return 1'b1;   // committed
        end
        m_pend[c] = 1; m_pgen[c] = g; m_pres[c] = res;
        return 1'b0;       // pended
    endfunction

    task automatic m_release(input [8:0] c);
        if (m_pins[c] > 0) begin
            m_pins[c] = m_pins[c] - 1;
            if ((m_pins[c] == 0) && m_pend[c]) begin
                m_pend[c] = 0; m_gen[c] = m_pgen[c]; m_resident[c] = m_pres[c];
            end
        end
    endtask

    //=========================================================================
    // Stimulus
    //=========================================================================
    logic ok;
    logic dummy;
    int   stale_payload;
    int   verdict_mismatches;
    int   trials;

    initial begin
        idle_inputs();
        m_reset();
        repeat (4) @(negedge clk);
        rst_n = 1;
        repeat (2) @(negedge clk);

        //=====================================================================
        $display("T1: reviewer interleaving (advisory read, update, CAS)");
        //=====================================================================
        // Descriptor names (chunk 42, generation 5); the pool holds <42,5,res>.
        place(9'd42, 16'd5, 1'b1);
        // Advisory stage: consult the entry, everything looks fine.
        adv_chunk = 9'd42;
        #0.1;
        check(adv_gen == 16'd5 && adv_resident && !adv_pending,
              "advisory read sees <42, gen 5, resident>");
        // The window: an update commits on the same entry before the CAS.
        place(9'd42, 16'd6, 1'b1);   // slot reused for generation 6
        check(dut.e_gen[42] == 16'd6, "update commits (gen 5 -> 6)");
        // The CAS stage now tries to install the pin for generation 5.
        do_cas(9'd42, 16'd5, ok);
        check(ok == 1'b0, "CAS rejects the stale descriptor");
        check(dut.e_pins[42] == '0, "no pin installed (zero payload possible)");
        check(dut.e_gen[42] == 16'd6, "directory keeps the new binding");

        //=====================================================================
        $display("T2: update after the linearization point");
        //=====================================================================
        place(9'd77, 16'd9, 1'b1);
        do_cas(9'd77, 16'd9, ok);
        check(ok == 1'b1, "CAS succeeds on the current binding");
        check(dut.e_pins[77] == 1, "pin installed");
        // Update arrives while pinned: it must not commit.
        @(negedge clk);
        upd_valid = 1; upd_chunk = 9'd77; upd_gen = 16'd10; upd_resident = 1'b1;
        #0.1;
        check(upd_committed == 1'b0 && upd_pended == 1'b1,
              "update diverts to pending_reclaim");
        @(negedge clk);
        upd_valid = 0;
        // While pending, a new pin on the same entry must be rejected.
        do_cas(9'd77, 16'd9, ok);
        check(ok == 1'b0, "pending_reclaim rejects a new pin");
        check(dut.e_pins[77] == 1, "pin count frozen under pending");
        // Drain: the last release commits the waiting update.
        do_release(9'd77);
        check(dut.e_pins[77] == '0 && dut.e_pend[77] == 1'b0,
              "drain clears pending_reclaim");
        check(dut.e_gen[77] == 16'd10 && dut.e_resident[77],
              "waiting update commits at zero pins");

        //=====================================================================
        $display("T3: randomized adversary inside the OAT window");
        //=====================================================================
        stale_payload     = 0;
        verdict_mismatches = 0;
        trials            = 4000;
        m_reset();
        rst_n = 0; repeat (2) @(negedge clk); rst_n = 1; repeat (2) @(negedge clk);

        for (int t = 0; t < trials; t++) begin
            logic [8:0]       c;
            logic [GEN_W-1:0] g0, g1;
            int               upd_delay, cas_delay;
            logic             do_upd, model_verdict, rtl_verdict, model_caswon;

            c  = $urandom_range(0, 63);
            g0 = $urandom_range(1, 8);
            g1 = g0 + $urandom_range(1, 3);   // reuse always bumps generation
            upd_delay = $urandom_range(0, 4);
            cas_delay = $urandom_range(0, 4);
            do_upd    = $urandom_range(0, 1);

            // Place the current binding, both in RTL and in the model.
            place(c, g0, 1'b1);
            m_gen[c] = g0; m_resident[c] = 1; m_pins[c] = 0; m_pend[c] = 0;

            // Advisory read (both sides agree it is only consult).
            adv_chunk = c;
            #0.1;

            // Interleave update and CAS with random delays inside the window.
            model_caswon = 1'b0;
            fork
                begin : adv_update
                    if (do_upd) begin
                        repeat (upd_delay) @(negedge clk);
                        @(negedge clk);
                        upd_valid = 1; upd_chunk = c; upd_gen = g1; upd_resident = 1'b1;
                        @(negedge clk);
                        upd_valid = 0;
                    end
                end
                begin : adv_cas
                    repeat (cas_delay) @(negedge clk);
                    @(negedge clk);
                    cas_valid = 1; cas_chunk = c; cas_gen = g0;
                    #0.1 rtl_verdict = cas_ok;
                    @(negedge clk);
                    cas_valid = 0;
                end
            join

            // Golden model: replay the same ordering decision. The RTL
            // arbitrates a same-cycle collision as CAS-first, so the model
            // evaluates the update after the CAS verdict when delays tie.
            if (!do_upd) begin
                model_verdict = m_cas(c, g0, 1'b1);
            end else if (upd_delay < cas_delay) begin
                dummy = m_update(c, g1, 1'b1, 1'b0);
                model_verdict = m_cas(c, g0, 1'b1);
            end else begin
                model_caswon = m_cas(c, g0, 1'b1);
                dummy = m_update(c, g1, 1'b1, model_caswon);
                model_verdict = model_caswon;
            end

            if (rtl_verdict !== model_verdict) begin
                verdict_mismatches = verdict_mismatches + 1;
                $display("  [FAIL] t=%0d c=%0d g0=%0d g1=%0d upd=%0b ud=%0d cd=%0d rtl=%b model=%b",
                         t, c, g0, g1, do_upd, upd_delay, cas_delay,
                         rtl_verdict, model_verdict);
            end

            // Stale payload check: a successful CAS must bind the generation
            // the descriptor names at the CAS cycle itself.
            if (rtl_verdict && (dut.e_gen[c] != g0))
                stale_payload = stale_payload + 1;

            // Release any installed pin to reset for the next trial.
            do_release(c);
            m_release(c);
            repeat (2) @(negedge clk);
        end

        $display("T3 summary: %0d trials, verdict mismatches = %0d, stale payload = %0d",
                 trials, verdict_mismatches, stale_payload);
        check(verdict_mismatches == 0, "RTL verdict stream matches golden model");
        check(stale_payload == 0, "no successful CAS binds a stale generation");

        //=====================================================================
        if (errors == 0) $display("ALL CHECKS PASSED");
        else             $display("%0d CHECK(S) FAILED", errors);
        $finish;
    end

endmodule
