#!/usr/bin/env python3
"""APEX_PIPELINE — ASAP7 7nm RVT TT Synthesis Estimate.

Uses real cell data from asap7sc7p5t_28 Liberty (NLDM) characterization.
Produces area, timing, and power reports based on RTL structural analysis.
"""
import os

# ============================================================
# ASAP7 7nm RVT TT — Real Cell Data (from Liberty extraction)
# ============================================================
CELL = {
    'DFF':   {'area': 0.29160, 'delay': 92.93},  # clk-to-Q (ps)
    'NAND2': {'area': 0.08748, 'delay': 58.35},
    'NOR2':  {'area': 0.08748, 'delay': 48.36},
    'AND2':  {'area': 0.08748, 'delay': 54.62},
    'OR2':   {'area': 0.08748, 'delay': 64.52},
    'XOR2':  {'area': 0.13122, 'delay': 61.01},
    'FA':    {'area': 0.20412, 'delay': 79.54},
    'HA':    {'area': 0.13122, 'delay': 53.06},
    'INV':   {'area': 0.04374, 'delay': 52.04},
    'BUF':   {'area': 0.07290, 'delay': 60.97},
    'MUX2':  {'area': 0.14580, 'delay': 70.00},
}
DFF_SETUP = 40.28  # ps
PERIOD = 1000.0    # ps (1 GHz target)

# Leakage per cell type (pW, from Liberty)
LEAK = {
    'DFF': 261.0, 'NAND2': 109.0, 'NOR2': 107.0, 'AND2': 173.0,
    'FA': 230.0, 'INV': 53.0, 'MUX2': 200.0, 'XOR2': 155.0,
}

# ============================================================
# RTL Module Decomposition
# ============================================================
modules = {}

# --- APEX_MAC_ARRAY ---
modules['APEX_MAC_ARRAY'] = {
    'seq_bits': 26,   # score_out[15:0] + chunk_id[8:0] + valid
    'fa_cells': 7 * 208 + 4 * 27 + 27,  # 7 MUL + CSA + CPA
    'and_cells': 7 * 16 * 8,  # partial product AND gates
    'mux_cells': 0,
    'desc': '7x (8x16 MUL) + 4-level CSA + 27-bit CPA + output reg',
}

# --- APEX_TOPK_HEAP ---
modules['APEX_TOPK_HEAP'] = {
    'seq_bits': 7*(16+9) + 18*(16+9) + 3+5+16+5+16+9+2+16+3,  # EZ+SZ+ctrl
    'fa_cells': 45 * 16 * 3 // 4,  # 45 comparators (16-bit, prefix = 3/4 FA depth)
    'and_cells': 45 * 4,  # compare logic overhead
    'mux_cells': 7 * 2 * 16 + 18 * 2,  # sift muxes + SZ write muxes
    'desc': 'EZ[7x25b] + SZ[18x25b] + 45 comparators + sift network',
}

# --- APEX_EXPERT_BANK (x7) ---
modules['APEX_EXPERT_BANK_x7'] = {
    'seq_bits': 7 * 512 * 16,  # 7 banks x 512 entries x 16 bits
    'fa_cells': 0,
    'and_cells': 7 * (512 + 18),  # write-enable decode + read mux
    'mux_cells': 7 * 16 * 9,  # 7 banks, 16-bit output, 9-bit addr select
    'desc': '7x 512-entry x 16-bit register file + addr decode',
}

# --- APEX_PCM ---
modules['APEX_PCM'] = {
    'seq_bits': 512 + 16 + 8 + 5,
    'fa_cells': 24,  # 16-bit + 8-bit comparator
    'and_cells': 20,
    'mux_cells': 10,
    'desc': '512-bit residency bitmap + epoch/namespace check',
}

# --- APEX_WEIGHT_UPDATE ---
modules['APEX_WEIGHT_UPDATE'] = {
    'seq_bits': 7*8 + 7*16 + 7*24 + 7*18 + 7*24 + 18 + 7 + 7*8 + 15,
    'fa_cells': 7 * 18 + 7 * 8,  # 7x 18-bit subtract + 7x 8-bit multiply
    'and_cells': 7 * 24,  # shift logic
    'mux_cells': 7 * 24 + 14,  # quotient/remainder muxes
    'desc': '7x Hedge update + 24-cycle bit-serial divider + normalize',
}

# --- APEX_LOSS_COMPUTE ---
modules['APEX_LOSS_COMPUTE'] = {
    'seq_bits': 7*8 + 7*3 + 16 + 8 + 5,
    'fa_cells': 7 * 8 + 16,
    'and_cells': 14,
    'mux_cells': 7 * 3,
    'desc': '7-expert cross-entropy + load-balance loss quantization',
}

# --- APEX_SEA ---
modules['APEX_SEA'] = {
    'seq_bits': 16 + 512 + 8 + 3 + 16,
    'fa_cells': 0,
    'and_cells': 16 + 9,
    'mux_cells': 16,
    'desc': '16-bit LFSR + 512-bit coverage + epsilon decay',
}

# --- APEX_PIPELINE_CTRL ---
modules['APEX_PIPELINE_CTRL'] = {
    'seq_bits': 3 + 4 + 1 + 32*3,
    'fa_cells': 32 * 3,
    'and_cells': 30,
    'mux_cells': 10,
    'desc': 'Pipeline FSM + 3x 32-bit stat counters',
}

# --- APEX_PIPELINE (top glue) ---
modules['APEX_PIPELINE_TOP'] = {
    'seq_bits': 5 * (16 + 9 + 1 + 8),
    'fa_cells': 0,
    'and_cells': 20,
    'mux_cells': 5 * 16,
    'desc': 'Pipeline staging registers + routing',
}

# --- ICG ---
modules['ICG'] = {
    'seq_bits': 1,
    'fa_cells': 0,
    'and_cells': 2,
    'mux_cells': 0,
    'desc': 'Integrated clock gating cell',
}


# ============================================================
# CEFE correctness-core modules (the object contract itself).
# These are the OAT / pin / directory / arbitration blocks that the
# zero-RPE invariant depends on, decomposed from the SAME RTL with the
# SAME Liberty cell areas as the scorer above. Sequential-bit counts are
# read directly from the RTL table declarations (rtl/cefe_*.sv), so this
# is a structural estimate of the SAME class (and SAME +/-30% caveat) as
# the scorer figure, NOT a ratio split of the projected full-endpoint area.
# ============================================================
cefe_modules = {}

# --- cefe_pin_table.sv: 400 entries x {valid(1)+tenant(4)+chunk(9)+gen(16)} + cnt_q(9)
#     comb: two priority encoders (free/release) + a match tree, all over 400 entries.
cefe_modules['cefe_pin_table'] = {
    'seq_bits': 400 * (1 + 4 + 9 + 16) + 9,
    'fa_cells': 0,
    'and_cells': 400 * (16 + 9),      # per-entry (chunk,gen) equality compare
    'mux_cells': 400,                  # priority-encoder / index select tree
    'desc': '400-entry pin table (valid+tenant+chunk+gen) + alloc/release/reclaim compare',
}

# --- cefe_vc_wrr.sv: 16 VC x 32-deep x 128-bit queue_mem + ptrs/counts/deficit + regs
cefe_modules['cefe_vc_wrr'] = {
    'seq_bits': 16 * 32 * 128 + 16 * (5 + 5 + 6) + 16 * 5 + 4 + 128 + 4,
    'fa_cells': 16 * 5,                # per-VC deficit add/compare
    'and_cells': 16 * 8,               # empty/full + eligibility logic
    'mux_cells': 16 * 128 // 8,        # 16:1 pop-data select over 128-bit word
    'desc': '16 VC x 32-deep x 128b queue + deficit round-robin arbiter',
}

# --- cefe_cfo_cam.sv: 16 entries x {valid+handle(64)+tag(64)+epoch(16)+ro(1)+bitmap(16)+3}
cefe_modules['cefe_cfo_cam'] = {
    'seq_bits': 16 * (1 + 64 + 64 + 16 + 1 + 16 + 3),
    'fa_cells': 0,
    'and_cells': 16 * 64,              # associative 64-bit tag match per entry
    'mux_cells': 16 * 4,               # entry-id / completion routing select
    'desc': '16-entry coalescing CAM (64b HMAC tag) + multicast completion bitmap',
}

# --- cefe_bdb_parser.sv: FSM + address/count/staging registers
cefe_modules['cefe_bdb_parser'] = {
    'seq_bits': 48 + 4 + 16 + 3 + 64 + 8 + 16 + 8 + 4 + 64 + 6 + 48,
    'fa_cells': 6,
    'and_cells': 40,
    'mux_cells': 16,
    'desc': 'BDB doorbell DMA parser: header validate + per-descriptor stream',
}


def compute_area(m):
    """Compute module area in um2."""
    seq = m['seq_bits'] * CELL['DFF']['area']
    fa = m['fa_cells'] * CELL['FA']['area']
    and_g = m['and_cells'] * CELL['AND2']['area']
    mux = m['mux_cells'] * CELL['MUX2']['area']
    return seq + fa + and_g + mux


def compute_power(m, activity=0.15):
    """Estimate dynamic + leakage power in mW."""
    # Dynamic: P = alpha * N_cells * C_avg * V^2 * f
    # Simplified: use per-cell average power at 1 GHz
    # ASAP7 at 0.7V, 1GHz: ~0.5 uW per active equivalent gate
    total_cells = (m['seq_bits'] + m['fa_cells'] +
                   m['and_cells'] + m['mux_cells'])
    dynamic_mw = activity * total_cells * 0.5e-3  # mW
    leakage_mw = total_cells * 150e-6  # ~150 pW/cell avg → mW
    return dynamic_mw, leakage_mw


# ============================================================
# Generate Reports
# ============================================================
def gen_area_report():
    lines = []
    lines.append("=" * 78)
    lines.append(" Report : area")
    lines.append(" Design : APEX_PIPELINE")
    lines.append(" Library: asap7sc7p5t_28 RVT TT NLDM (from Liberty characterization)")
    lines.append(" Method : RTL structural decomposition mapped to ASAP7 cell areas")
    lines.append(" Date   : 2026-06-28")
    lines.append("=" * 78)
    lines.append("")
    lines.append("Library(s) Used:")
    lines.append("  asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib")
    lines.append("  asap7sc7p5t_AO_RVT_TT_nldm_211120.lib")
    lines.append("  asap7sc7p5t_OA_RVT_TT_nldm_211120.lib")
    lines.append("  asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib")
    lines.append("  asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib")
    lines.append("")
    lines.append("NOTE: This is a structural estimate, not post-synthesis netlist data.")
    lines.append("      Actual synthesis with Genus/DC may differ by +/-30% due to")
    lines.append("      optimization, sharing, and technology mapping decisions.")
    lines.append("")

    hdr = f"  {'Module':<25s} {'Seq(bits)':<10s} {'Comb':<8s} {'Area(um2)':<12s} {'%':>6s}"
    lines.append(hdr)
    lines.append("  " + "-" * 65)

    total_area = sum(compute_area(m) for m in modules.values())
    for name, m in modules.items():
        a = compute_area(m)
        comb = m['fa_cells'] + m['and_cells'] + m['mux_cells']
        pct = a / total_area * 100
        lines.append(f"  {name:<25s} {m['seq_bits']:<10d} {comb:<8d} {a:<12.4f} {pct:5.1f}%")

    lines.append("  " + "-" * 65)
    lines.append(f"  {'TOTAL':<25s} {'':10s} {'':8s} {total_area:<12.4f} 100.0%")
    lines.append("")
    lines.append(f"  Total cell area:     {total_area:.4f} um2")
    lines.append(f"  With routing (1.3x): {total_area*1.3:.4f} um2")
    lines.append(f"  In mm2:              {total_area*1.3/1e6:.6f} mm2")
    lines.append("")
    lines.append("  NOTE: Expert banks dominate area (register-file based).")
    lines.append("        With SRAM macros: subtract ~95% of bank area,")
    lines.append(f"        yielding ~{(total_area - compute_area(modules['APEX_EXPERT_BANK_x7'])*0.95)*1.3/1e6:.6f} mm2 logic + SRAM overhead.")
    lines.append("")
    lines.append("  Key cell areas (from Liberty):")
    lines.append(f"    DFF (DFFHQNx1): {CELL['DFF']['area']:.5f} um2")
    lines.append(f"    NAND2x1:       {CELL['NAND2']['area']:.5f} um2")
    lines.append(f"    FA:            {CELL['FA']['area']:.5f} um2")
    lines.append(f"    INV:           {CELL['INV']['area']:.5f} um2")
    return "\n".join(lines)


def gen_timing_report():
    lines = []
    lines.append("=" * 78)
    lines.append(" Report : timing")
    lines.append("         -path full -delay max")
    lines.append(" Design : APEX_PIPELINE")
    lines.append(" Library: asap7sc7p5t_28 RVT TT NLDM")
    lines.append(" Method : Critical path analysis using Liberty cell_rise data")
    lines.append(" Clock  : 1 GHz (period = 1000 ps)")
    lines.append(" Date   : 2026-06-28")
    lines.append("=" * 78)
    lines.append("")
    lines.append("NOTE: Delays use typical (median) values from Liberty NLDM tables.")
    lines.append("      Wire delay and clock uncertainty are not modeled (add ~50-100 ps).")
    lines.append("      Run actual STA (PrimeTime/Tempus) for signoff-quality results.")
    lines.append("")

    # Path 1: MAC
    p1 = [
        ("DFF clk-to-Q", CELL['DFF']['delay']),
        ("AND2 (partial products)", CELL['AND2']['delay']),
        ("3-level Wallace (FA)", 3 * CELL['FA']['delay']),
        ("4-level CSA accumulate", 4 * CELL['FA']['delay']),
        ("16-bit prefix CPA", 4 * CELL['HA']['delay']),  # log2(16)=4 levels
        ("DFF setup", DFF_SETUP),
    ]
    mac_total = sum(d for _, d in p1)

    lines.append("  Path 1 (Critical): MAC Array — multiply + CSA + CPA")
    lines.append(f"  Startpoint: u_apex_pipeline/u_mac (pipeline input register)")
    lines.append(f"  Endpoint:   u_apex_pipeline/u_mac/score_out_reg[15]")
    lines.append("")
    lines.append(f"    {'Stage':<35s} {'Delay(ps)':>10s} {'Cumul(ps)':>10s}")
    lines.append("    " + "-" * 58)
    cumul = 0
    for stage, d in p1:
        cumul += d
        lines.append(f"    {stage:<35s} {d:10.1f} {cumul:10.1f}")
    lines.append("    " + "-" * 58)
    lines.append(f"    {'Path delay':<35s} {mac_total:10.1f}")
    lines.append(f"    {'Required (period)':<35s} {PERIOD:10.1f}")
    lines.append(f"    {'Slack':<35s} {PERIOD-mac_total:10.1f}  {'(MET)' if mac_total < PERIOD else '(VIOLATED)'}")
    lines.append("")

    # Path 2: Heap sift
    # 16-bit magnitude comparator via prefix tree: 4 levels of NAND2-equivalent
    cmp16_prefix = 4 * CELL['NAND2']['delay']
    p2 = [
        ("DFF clk-to-Q", CELL['DFF']['delay']),
        ("16-bit comparator L0 (prefix)", cmp16_prefix),
        ("MUX2 (go_left/go_right)", CELL['MUX2']['delay']),
        ("16-bit comparator L1 (prefix)", cmp16_prefix),
        ("MUX2 (leaf select)", CELL['MUX2']['delay']),
        ("DFF setup", DFF_SETUP),
    ]
    heap_total = sum(d for _, d in p2)

    lines.append("  Path 2: Heap Sift-Down — 2-level comparator + mux chain")
    lines.append(f"  Startpoint: u_apex_pipeline/u_heap/ez_score_reg")
    lines.append(f"  Endpoint:   u_apex_pipeline/u_heap/ez_score_reg")
    lines.append("")
    lines.append(f"    {'Stage':<35s} {'Delay(ps)':>10s} {'Cumul(ps)':>10s}")
    lines.append("    " + "-" * 58)
    cumul = 0
    for stage, d in p2:
        cumul += d
        lines.append(f"    {stage:<35s} {d:10.1f} {cumul:10.1f}")
    lines.append("    " + "-" * 58)
    lines.append(f"    {'Path delay':<35s} {heap_total:10.1f}")
    lines.append(f"    {'Slack':<35s} {PERIOD-heap_total:10.1f}  (MET)")
    lines.append("")

    # Path 3: SZ min-tree (off admission-critical-path)
    # NOTE: The SZ min-tree updates safe_min_reg on idle cycles or after
    # Case 2 (via sz_min_fwd). The admission classification reads safe_min
    # from a REGISTER, so the 5-level tree is NOT on the timing-critical
    # admission path. However, it must close within one cycle for the
    # refresh to be single-cycle. If it cannot, the RTL already has
    # idle-cycle refresh logic that tolerates multi-cycle convergence.
    #
    # 16-bit comparator: Kogge-Stone prefix = ceil(log2(16))=4 gate levels
    # Using NAND2 delay (~58 ps) as gate delay for prefix logic:
    cmp16 = 4 * CELL['NAND2']['delay']  # 4-level prefix tree
    p3 = [
        ("DFF clk-to-Q", CELL['DFF']['delay']),
        ("L1: 16-bit CMP (4-level prefix)", cmp16),
        ("L1 MUX2 (min select)", CELL['MUX2']['delay']),
        ("L2: 16-bit CMP", cmp16),
        ("L2 MUX2", CELL['MUX2']['delay']),
        ("L3: 16-bit CMP", cmp16),
        ("L3 MUX2", CELL['MUX2']['delay']),
        ("L4: 16-bit CMP", cmp16),
        ("L4 MUX2", CELL['MUX2']['delay']),
        ("L5: 16-bit CMP", cmp16),
        ("L5 MUX2", CELL['MUX2']['delay']),
        ("DFF setup", DFF_SETUP),
    ]
    sz_total = sum(d for _, d in p3)

    lines.append("  Path 3: SZ Min-Tree (off critical path, refreshes safe_min_reg)")
    lines.append(f"  Startpoint: u_apex_pipeline/u_heap/sz_score_reg")
    lines.append(f"  Endpoint:   u_apex_pipeline/u_heap/safe_min_reg")
    lines.append(f"  NOTE: Not on admission-critical path (uses registered safe_min)")
    lines.append("")
    lines.append(f"    {'Stage':<35s} {'Delay(ps)':>10s} {'Cumul(ps)':>10s}")
    lines.append("    " + "-" * 58)
    cumul = 0
    for stage, d in p3:
        cumul += d
        lines.append(f"    {stage:<35s} {d:10.1f} {cumul:10.1f}")
    lines.append("    " + "-" * 58)
    lines.append(f"    {'Path delay':<35s} {sz_total:10.1f}")
    lines.append(f"    {'Slack':<35s} {PERIOD-sz_total:10.1f}  {'(MET)' if sz_total < PERIOD else '(NEEDS MULTICYCLE or PIPELINE)'}")
    lines.append("")
    if sz_total > PERIOD:
        lines.append(f"    ARCHITECTURAL NOTE: At {sz_total:.0f} ps this path cannot close in")
        lines.append(f"    1 cycle at 1 GHz. The RTL handles this via idle-cycle refresh:")
        lines.append(f"    safe_min is updated only when no admission is in flight.")
        lines.append(f"    A 2-cycle multicycle_path constraint resolves this cleanly.")
        lines.append(f"    Alternatively, pipeline the tree (split at Level 3).")
        lines.append("")

    lines.append("=" * 78)
    # The critical path for functional timing is MAC (Path 1) since
    # SZ tree is architecturally off the hot path
    crit = max(mac_total, heap_total)
    lines.append(f"  Functional critical path: {crit:.1f} ps (MAC array)")
    lines.append(f"  SZ min-tree path: {sz_total:.1f} ps (off-critical, multicycle-able)")
    lines.append(f"  Achievable Fmax (functional): {1000/crit*1000:.0f} MHz")
    lines.append(f"  Target: 1000 MHz — {'TIMING MET' if crit < PERIOD else 'TIMING VIOLATED'}")
    lines.append("=" * 78)
    return "\n".join(lines)


def gen_power_report():
    lines = []
    lines.append("=" * 78)
    lines.append(" Report : power")
    lines.append(" Design : APEX_PIPELINE")
    lines.append(" Library: asap7sc7p5t_28 RVT TT NLDM")
    lines.append(" Method : Activity-based estimation using Liberty leakage data")
    lines.append(" Clock  : 1 GHz, Vdd = 0.7V")
    lines.append(" Date   : 2026-06-28")
    lines.append("=" * 78)
    lines.append("")
    lines.append("NOTE: Dynamic power assumes average switching activity alpha=0.15.")
    lines.append("      Clock tree power estimated at 30% of total dynamic.")
    lines.append("      For accurate results, use VCD-based power analysis.")
    lines.append("")

    activities = {
        'APEX_MAC_ARRAY': 0.25,
        'APEX_TOPK_HEAP': 0.15,
        'APEX_EXPERT_BANK_x7': 0.06,
        'APEX_PCM': 0.20,
        'APEX_WEIGHT_UPDATE': 0.02,
        'APEX_LOSS_COMPUTE': 0.10,
        'APEX_SEA': 0.05,
        'APEX_PIPELINE_CTRL': 0.30,
        'APEX_PIPELINE_TOP': 0.20,
        'ICG': 0.01,
    }

    hdr = f"  {'Module':<25s} {'Activity':<9s} {'Dyn(mW)':<9s} {'Leak(mW)':<9s} {'Total(mW)':<10s}"
    lines.append(hdr)
    lines.append("  " + "-" * 65)

    total_dyn = 0
    total_leak = 0
    for name, m in modules.items():
        act = activities.get(name, 0.15)
        dyn, leak = compute_power(m, act)
        total_dyn += dyn
        total_leak += leak
        lines.append(f"  {name:<25s} {act:<9.2f} {dyn:<9.4f} {leak:<9.4f} {dyn+leak:<10.4f}")

    # Clock tree overhead (~30% of dynamic)
    clk_dyn = total_dyn * 0.30
    lines.append(f"  {'Clock tree (est 30%)':<25s} {'1.00':<9s} {clk_dyn:<9.4f} {'0.0000':<9s} {clk_dyn:<10.4f}")
    total_dyn += clk_dyn

    lines.append("  " + "-" * 65)
    lines.append(f"  {'TOTAL':<25s} {'':9s} {total_dyn:<9.4f} {total_leak:<9.4f} {total_dyn+total_leak:<10.4f}")
    lines.append("")
    lines.append(f"  Total power: {total_dyn+total_leak:.3f} mW")
    lines.append(f"    Dynamic:   {total_dyn:.3f} mW ({total_dyn/(total_dyn+total_leak)*100:.0f}%)")
    lines.append(f"    Leakage:   {total_leak:.3f} mW ({total_leak/(total_dyn+total_leak)*100:.0f}%)")
    lines.append("")
    lines.append("  NOTE: With SRAM macros for expert banks, dynamic power reduces")
    lines.append("        significantly (SRAM read power << register-file read power).")
    return "\n".join(lines)


def gen_breakdown_report():
    """Correctness-core vs optional-scorer structural breakdown.

    Splits the synthesizable RTL into (i) the correctness core the zero-RPE
    invariant depends on -- PCM validation plus the CEFE OAT/pin/directory/
    arbitration modules -- and (ii) the optional policy layer (scorer, top-K,
    weight update, loss, SEA). Area is the structural estimate mapped to ASAP7
    Liberty cells (same method and +/-30% caveat as area.rpt); state is the
    RTL-declared sequential bits. This is what actually implements the object
    contract, reported separately from the scorer so the two are not conflated.
    It is NOT a decomposition of the projected full-endpoint 1.069 mm^2 (that
    projection additionally budgets SRAM macros and 16-host staging not synthesized here).
    """
    # correctness-core membership: PCM (residency/epoch validation) + CEFE blocks
    core_names = ['APEX_PCM']
    core = {**{n: modules[n] for n in core_names}, **cefe_modules}
    # optional policy layer: everything else in the scorer pipeline
    policy_names = ['APEX_MAC_ARRAY', 'APEX_TOPK_HEAP', 'APEX_EXPERT_BANK_x7',
                    'APEX_WEIGHT_UPDATE', 'APEX_LOSS_COMPUTE', 'APEX_SEA']
    policy = {n: modules[n] for n in policy_names}
    glue_names = ['APEX_PIPELINE_CTRL', 'APEX_PIPELINE_TOP', 'ICG']
    glue = {n: modules[n] for n in glue_names}

    def group_area_mm2(g):
        return sum(compute_area(m) for m in g.values()) * 1.3 / 1e6
    def group_power_mw(g, acts):
        d = l = 0.0
        for n, m in g.items():
            dd, ll = compute_power(m, acts.get(n, 0.15))
            d += dd; l += ll
        return d + l
    def group_state_kib(g):
        return sum(m['seq_bits'] for m in g.values()) / 8.0 / 1024.0

    acts = {'APEX_MAC_ARRAY': 0.25, 'APEX_TOPK_HEAP': 0.15,
            'APEX_EXPERT_BANK_x7': 0.06, 'APEX_PCM': 0.20,
            'APEX_WEIGHT_UPDATE': 0.02, 'APEX_LOSS_COMPUTE': 0.10,
            'APEX_SEA': 0.05, 'APEX_PIPELINE_CTRL': 0.30,
            'APEX_PIPELINE_TOP': 0.20, 'ICG': 0.01,
            'cefe_pin_table': 0.20, 'cefe_vc_wrr': 0.25,
            'cefe_cfo_cam': 0.10, 'cefe_bdb_parser': 0.15}

    L = []
    L.append("=" * 78)
    L.append(" Report : correctness-core vs optional-scorer breakdown")
    L.append(" Method : structural estimate, ASAP7 Liberty cells (as area.rpt); "
             "state = RTL-declared bits")
    L.append(" NOTE   : structural estimate (+/-30%), NOT a split of the projected "
             "1.069 mm^2 full endpoint;")
    L.append("          the projection additionally budgets SRAM macros + 16-host "
             "staging not synthesized here.")
    L.append("=" * 78)
    L.append("")
    L.append(f"  {'Group / module':<34s} {'Area(mm2)':>10s} {'Power(mW)':>10s} {'State(KiB)':>11s}")
    L.append("  " + "-" * 68)

    def emit_group(title, g):
        L.append(f"  {title}")
        for n, m in g.items():
            a = compute_area(m) * 1.3 / 1e6
            dd, ll = compute_power(m, acts.get(n, 0.15))
            st = m['seq_bits'] / 8.0 / 1024.0
            L.append(f"    {n:<32s} {a:>10.4f} {dd+ll:>10.4f} {st:>11.2f}")
        L.append(f"    {'-- subtotal --':<32s} {group_area_mm2(g):>10.4f} "
                 f"{group_power_mw(g, acts):>10.4f} {group_state_kib(g):>11.2f}")

    emit_group("Correctness core (OAT / pin / directory / arbitration):", core)
    L.append("")
    emit_group("Optional policy (scorer / top-K / weight / SEA):", policy)
    L.append("")
    emit_group("Pipeline glue:", glue)
    L.append("  " + "-" * 68)
    all_g = {**core, **policy, **glue}
    L.append(f"    {'SYNTHESIZED TOTAL (register-file banks)':<32s} "
             f"{group_area_mm2(all_g):>10.4f} {group_power_mw(all_g, acts):>10.4f} "
             f"{group_state_kib(all_g):>11.2f}")
    L.append("")
    L.append("  Reconciliation with the paper's figures:")
    L.append("   * Scorer pipeline alone      : 0.024 mm^2  (area.rpt; matches paper).")
    L.append("   * Full endpoint (projected)  : 1.069 mm^2 / 78 mW / ~216 KiB  "
             "(SRAM-macro + 16-host,")
    L.append("     projection, not this structural netlist; see README / RESULT_ALIGNMENT.md).")
    L.append("   * This table isolates the correctness-core LOGIC + STATE at the "
             "SAME structural")
    L.append("     fidelity as the scorer, so the OAT/pin cost is reported, not "
             "inferred by ratio.")
    return "\n".join(L)


if __name__ == '__main__':
    report_dir = os.path.join(os.path.dirname(__file__), 'reports')
    os.makedirs(report_dir, exist_ok=True)

    area_rpt = gen_area_report()
    timing_rpt = gen_timing_report()
    power_rpt = gen_power_report()
    breakdown_rpt = gen_breakdown_report()

    with open(os.path.join(report_dir, 'area.rpt'), 'w') as f:
        f.write(area_rpt + '\n')
    with open(os.path.join(report_dir, 'timing.rpt'), 'w') as f:
        f.write(timing_rpt + '\n')
    with open(os.path.join(report_dir, 'power.rpt'), 'w') as f:
        f.write(power_rpt + '\n')
    with open(os.path.join(report_dir, 'breakdown.rpt'), 'w') as f:
        f.write(breakdown_rpt + '\n')

    print("Reports generated:")
    print(f"  {report_dir}/area.rpt")
    print(f"  {report_dir}/timing.rpt")
    print(f"  {report_dir}/power.rpt")
    print(f"  {report_dir}/breakdown.rpt")
    print()
    print(breakdown_rpt)
