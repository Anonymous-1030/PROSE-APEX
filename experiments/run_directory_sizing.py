#!/usr/bin/env python3
"""Directory-sizing analysis for the PROSE endpoint object directory.

Computes, from the RTL parameters parsed out of rtl/cefe_addr_mapper.sv and
rtl/cefe_pin_table.sv (no hand-entered results):

  1. Object universe of a 1M-token-context deployment:
       1M tokens / 64-token chunks = 16,384 chunks/host x 16 hosts
       = 262,144 objects.
  2. Naive flat directory over the full universe vs. the 216 KiB on-chip
     state budget (deficit).
  3. The two-tier mapper exactly as implemented (SRAM bits, coverage) and
     the same two-tier architecture scaled to cover 262,144 objects
     (fit/gap verdict + minimum budget that fits).
  4. Resident-set insight: the authoritative binding only needs to cover
     the RESIDENT set (pool slots), not the object universe. Directory
     bits for pool sizes {512, 4096, 16384, 65536} and the crossover pool
     size where the directory exceeds 216 KiB.
  5. Aliasing / false-admit bound for the two-tier hash (birthday-style,
     using the mapper's actual tag width) and the safety consequence once
     the OAT's 16-bit generation check is factored in.

Output:
  results/directory_sizing.json
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results"
OUT.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Parse RTL parameters (source of truth: the .sv files, not this script).
# ---------------------------------------------------------------------------
PARAM_RE = re.compile(r"parameter\s+int\s+(\w+)\s*=\s*(\d+)")


def parse_params(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    return {m.group(1): int(m.group(2)) for m in PARAM_RE.finditer(text)}


mapper = parse_params(ROOT / "rtl" / "cefe_addr_mapper.sv")
pin = parse_params(ROOT / "rtl" / "cefe_pin_table.sv")

for needed in ("LOGICAL_ID_W", "HOT_ENTRIES", "BACKING_ENTRIES", "PTR_W"):
    assert needed in mapper, f"missing parameter {needed} in cefe_addr_mapper.sv"
for needed in ("NUM_ENTRIES", "TENANT_W", "CHUNK_W", "GEN_W"):
    assert needed in pin, f"missing parameter {needed} in cefe_pin_table.sv"

# Mirror the RTL localparams (cefe_addr_mapper.sv L69-71).
LOGICAL_ID_W = mapper["LOGICAL_ID_W"]          # 12
HOT_ENTRIES = mapper["HOT_ENTRIES"]            # 512
BACKING_ENTRIES = mapper["BACKING_ENTRIES"]    # 2048
PTR_W = mapper["PTR_W"]                        # 20
HOT_IDX_W = HOT_ENTRIES.bit_length() - 1       # $clog2(512)  = 9
BK_IDX_W = BACKING_ENTRIES.bit_length() - 1    # $clog2(2048) = 11
TAG_W = LOGICAL_ID_W - HOT_IDX_W               # RTL TAG_W    = 3

GEN_W = pin["GEN_W"]                           # 16 (OAT-validated generation)
PIN_ENTRY_BITS = 1 + pin["TENANT_W"] + pin["CHUNK_W"] + pin["GEN_W"]  # 30
PIN_TABLE_BITS = pin["NUM_ENTRIES"] * PIN_ENTRY_BITS                  # 12,000

# ---------------------------------------------------------------------------
# Given constraints (deployment parameters and the budget — not results).
# ---------------------------------------------------------------------------
TOKENS_PER_CONTEXT = 1 << 20        # 1M-token context deployment (2^20)
CHUNK_TOKENS = 64                   # 64-token chunks (paper)
HOSTS = 16                          # pooled hosts
BUDGET_KIB = 216                    # on-chip state budget (both RTL headers)
BUDGET_BITS = BUDGET_KIB * 1024 * 8

KiB = lambda bits: bits / (1024 * 8)

# ---------------------------------------------------------------------------
# 1. Object universe
# ---------------------------------------------------------------------------
chunks_per_host = TOKENS_PER_CONTEXT // CHUNK_TOKENS          # 16,384
universe = chunks_per_host * HOSTS                            # 262,144
OBJ_ID_W = math.ceil(math.log2(universe))                     # 18

# ---------------------------------------------------------------------------
# 2. Naive flat directory over the full universe
# ---------------------------------------------------------------------------
# (a) Identity-validating flat directory (mapper entry format {valid,tag,ptr}
#     with the tag widened to the full object id so an entry can actually
#     validate identity): 1 + 18 + 20 = 39 b/entry.
flat_tag_entry_bits = 1 + OBJ_ID_W + PTR_W
flat_tag_bits = universe * flat_tag_entry_bits
# (b) Literal mapper Tier-1 format {valid, TAG_W, ptr} scaled naively — an
#     (insufficient) lower bound, since a 3-bit tag cannot validate 2^18 ids.
flat_literal_entry_bits = 1 + TAG_W + PTR_W
flat_literal_bits = universe * flat_literal_entry_bits
# (c) Direct-indexed flat array (index = full id, no tag needed):
#     {valid, ptr} = 21 b/entry — the cheapest exact full-universe structure.
flat_direct_entry_bits = 1 + PTR_W
flat_direct_bits = universe * flat_direct_entry_bits

naive = {
    "tag_validating": {
        "formula": "U * (1 + OBJ_ID_W + PTR_W) = 262144 * (1 + 18 + 20)",
        "entry_bits": flat_tag_entry_bits,
        "total_bits": flat_tag_bits,
        "total_KiB": KiB(flat_tag_bits),
        "deficit_KiB": KiB(flat_tag_bits - BUDGET_BITS),
        "over_budget_factor": flat_tag_bits / BUDGET_BITS,
    },
    "literal_mapper_format_lower_bound": {
        "formula": "U * (1 + TAG_W + PTR_W) = 262144 * (1 + 3 + 20)  "
                   "(3-bit tag cannot validate 2^18 ids -> not a viable directory)",
        "entry_bits": flat_literal_entry_bits,
        "total_bits": flat_literal_bits,
        "total_KiB": KiB(flat_literal_bits),
        "deficit_KiB": KiB(flat_literal_bits - BUDGET_BITS),
    },
    "direct_indexed_exact": {
        "formula": "U * (1 + PTR_W) = 262144 * 21  "
                   "(index = full 18-bit id, no tag, exact by construction)",
        "entry_bits": flat_direct_entry_bits,
        "total_bits": flat_direct_bits,
        "total_KiB": KiB(flat_direct_bits),
        "deficit_KiB": KiB(flat_direct_bits - BUDGET_BITS),
    },
}

# ---------------------------------------------------------------------------
# 3. Two-tier mapper: as implemented, and scaled to the 262,144-object universe
# ---------------------------------------------------------------------------
t1_entry_bits = 1 + TAG_W + PTR_W                    # 24 (as coded)
t2_entry_bits = 1 + PTR_W                            # 21 (tagless!)
t1_bits = HOT_ENTRIES * t1_entry_bits                # 12,288
t2_bits = BACKING_ENTRIES * t2_entry_bits            # 43,008
mapper_bits = t1_bits + t2_bits                      # 55,296
mapper_universe = 1 << LOGICAL_ID_W                  # 4,096

# Active working set per decode step (S10 / mapper header): 200-400 chunks.
active_lo, active_hi = 200, 400

# Scaled two-tier covering the full universe: same architecture, Tier-2 must
# hold every object. Tier-1 tag widens to OBJ_ID_W - HOT_IDX_W; Tier-2 stays
# a direct-mapped/direct-indexed {valid, ptr} array over the full id space.
t1s_tag_w = OBJ_ID_W - HOT_IDX_W                     # 9
t1s_bits = HOT_ENTRIES * (1 + t1s_tag_w + PTR_W)     # 15,360
t2s_bits = universe * (1 + PTR_W)                    # 5,505,024
scaled_bits = t1s_bits + t2s_bits                    # 5,520,384

two_tier = {
    "as_implemented": {
        "formula": "HOT_ENTRIES*(1+TAG_W+PTR_W) + BACKING_ENTRIES*(1+PTR_W) "
                   "= 512*24 + 2048*21",
        "t1_entry_bits": t1_entry_bits,
        "t2_entry_bits": t2_entry_bits,
        "t1_bits": t1_bits,
        "t2_bits": t2_bits,
        "total_bits": mapper_bits,
        "total_KiB": KiB(mapper_bits),
        "fits_budget": mapper_bits <= BUDGET_BITS,
        "designed_universe": mapper_universe,
        "t2_fold_factor_at_262144": universe // BACKING_ENTRIES,
        "coverage_verdict": (
            "Fits 216 KiB trivially but covers only a 2^12 = 4,096-object "
            "universe. Tier-2 is direct-mapped by the low 11 id bits with NO "
            "tag (cefe_addr_mapper.sv L95-96, L140), so at 262,144 objects "
            "128 objects fold onto every backing slot with zero validation. "
            "It is a translation cache, not an authoritative directory."
        ),
    },
    "scaled_to_universe": {
        "formula": "HOT_ENTRIES*(1+(OBJ_ID_W-HOT_IDX_W)+PTR_W) + U*(1+PTR_W) "
                   "= 512*30 + 262144*21",
        "t1_bits": t1s_bits,
        "t2_bits": t2s_bits,
        "total_bits": scaled_bits,
        "total_KiB": KiB(scaled_bits),
        "fits_budget": scaled_bits <= BUDGET_BITS,
        "deficit_KiB": KiB(scaled_bits - BUDGET_BITS),
        "min_budget_KiB": KiB(scaled_bits),
        "load_factor_assumption": (
            "Tier-1 holds the 200-400 active chunks/step (load 0.39-0.78 of "
            "512); Tier-2 runs at load factor 1.0 (every object has exactly "
            "one home, overwrite eviction) -- statistical, not guaranteed, "
            "coverage."
        ),
        "verdict": (
            "Does NOT fit: needs 673.9 KiB, a 457.9 KiB gap over the 216 KiB "
            "budget. Minimum budget for full-universe exact coverage "
            "(direct-indexed backing + hot tier) is ~674 KiB, 3.1x budget."
        ),
    },
}

# ---------------------------------------------------------------------------
# 4. Resident-set directory: bind only what is actually in the pool
# ---------------------------------------------------------------------------
# Entry = {valid 1b, object-id OBJ_ID_W (exact match), generation GEN_W,
#          slot-ptr ceil(log2 S)}. Full-id + generation compare => zero
# aliasing by construction (this is exactly the OAT binding the pin table
# implements at batch scope: 400 x 30b = 1.47 KiB).
def resident_entry_bits(slots: int) -> int:
    ptr_w = max(1, math.ceil(math.log2(slots)))
    return 1 + OBJ_ID_W + GEN_W + ptr_w


pool_sweep = {}
for slots in (512, 4096, 16384, 65536):
    eb = resident_entry_bits(slots)
    total = slots * eb
    pool_sweep[str(slots)] = {
        "entry_bits": eb,
        "total_bits": total,
        "total_KiB": KiB(total),
        "fits_budget": total <= BUDGET_BITS,
    }

# Crossover: largest pool whose directory still fits the budget.
max_fit = max(
    s for s in range(1, 1 << 21)
    if s * resident_entry_bits(s) <= BUDGET_BITS
)
largest_pow2 = 1 << (max_fit.bit_length() - 1)
# Continuous approximation of the crossover (ignoring the ceil in ptr width):
# S * (35 + log2 S) = BUDGET_BITS  ->  S = B / (35 W-ish); solve by bisection.
lo, hi = 1.0, float(1 << 21)
for _ in range(200):
    mid = (lo + hi) / 2
    if mid * (1 + OBJ_ID_W + GEN_W + math.log2(mid)) <= BUDGET_BITS:
        lo = mid
    else:
        hi = mid
crossover_continuous = lo

resident = {
    "entry_formula": "1 (valid) + 18 (full object id) + 16 (generation) "
                     "+ ceil(log2 S) (slot pointer)",
    "pool_sweep": pool_sweep,
    "crossover_slots_integer": max_fit,
    "crossover_slots_continuous": round(crossover_continuous, 1),
    "largest_power_of_two_that_fits": largest_pow2,
    "KiB_at_largest_pow2": KiB(largest_pow2 * resident_entry_bits(largest_pow2)),
    "pin_table_crosscheck": {
        "formula": "400 * (1+4+9+16) = 12,000 b (batch-scope instance of the "
                   "same resident-binding idea, cefe_pin_table.sv L28-29)",
        "total_bits": PIN_TABLE_BITS,
        "total_KiB": KiB(PIN_TABLE_BITS),
    },
}

# ---------------------------------------------------------------------------
# 5. Aliasing / false-admit bound (birthday-style on the mapper's tag bits)
# ---------------------------------------------------------------------------
def birthday_expected_pairs(n: int, space: int) -> float:
    return n * (n - 1) / (2.0 * space)


def per_lookup_alias_prob(resident_n: int, space: int) -> float:
    return 1.0 - (1.0 - 1.0 / space) ** resident_n


# (a) Mapper as designed: 4,096-id universe, S=512 slots, t=3-bit tag.
space_design = HOT_ENTRIES * (1 << TAG_W)             # 4,096
pairs_design = birthday_expected_pairs(mapper_universe, space_design)
p_lookup_active = per_lookup_alias_prob(active_hi, space_design)   # 400 active

# (b) Same structure widened to the 18-bit universe: t = 18-9 = 9.
space_scaled = HOT_ENTRIES * (1 << t1s_tag_w)         # 262,144
pairs_scaled = birthday_expected_pairs(universe, space_scaled)
p_lookup_scaled = per_lookup_alias_prob(universe, space_scaled)

# (c) Tier-2 tagless backing: aliasing is CERTAIN once U > 2048.
t2_objects_per_slot = universe / BACKING_ENTRIES      # 128

# (d) Safety consequence: an alias becomes a FALSE ADMIT only if the OAT's
#     generation check also passes: same Tier-1 slot AND colliding tag AND
#     same 16-bit generation simultaneously.
hazard_pairs_scaled = pairs_scaled / (1 << GEN_W)     # ~2.0
hazard_pairs_design = pairs_design / (1 << GEN_W)     # ~0.031
p_false_admit_lookup = p_lookup_scaled / (1 << GEN_W)

aliasing = {
    "tag_w_as_coded": TAG_W,
    "header_comment_note": (
        "Header L32 documents {valid, tag[10:0], ptr[19:0]} (11-bit tag); the "
        "actual RTL localparam TAG_W = LOGICAL_ID_W - HOT_IDX_W = 12 - 9 = 3 "
        "(L69-71, L83-85). The header comment is stale; the analysis uses the "
        "as-coded 3-bit tag (and the 9-bit tag of the 18-bit-widened variant)."
    ),
    "as_designed_4096_universe": {
        "slot_tag_space": space_design,
        "expected_alias_pairs_full_universe": pairs_design,
        "per_lookup_alias_prob_at_400_active": p_lookup_active,
        "expected_false_admit_pairs_after_gen_check": hazard_pairs_design,
    },
    "scaled_262144_universe": {
        "tag_w_if_widened": t1s_tag_w,
        "slot_tag_space": space_scaled,
        "expected_alias_pairs_full_universe": pairs_scaled,
        "per_lookup_alias_prob": p_lookup_scaled,
        "tier2_objects_per_slot_tagless": t2_objects_per_slot,
        "expected_false_admit_pairs_after_gen_check": hazard_pairs_scaled,
        "per_lookup_false_admit_prob": p_false_admit_lookup,
    },
    "false_admit_chain": (
        "P(false admit) = P(same Tier-1 slot) x P(colliding tag) x P(same "
        "16-bit generation). An alias alone returns a wrong pointer — a "
        "performance/Recovery issue. It becomes a correctness issue (stale "
        "payload admitted) ONLY if the aliased entry also carries the same "
        "16-bit generation the OAT is validating: suppression factor 2^-16. "
        "At the 262,144-object universe ~131,072 aliasing pairs are expected "
        "in the hot tier, of which ~2.0 would ALSO share a generation by "
        "chance — so the two-tier hash as-implemented cannot serve as the "
        "authoritative binding at 1M-token scale."
    ),
    "resident_directory_safety": (
        "The resident-set directory stores the full 18-bit object id + 16-bit "
        "generation and validates by exact compare: aliasing is zero by "
        "construction (two distinct objects always differ in id). The only "
        "residual hazard is generation reuse on the SAME id across 2^16 "
        "reclaims (ABA), suppressed at 2^-16 per reuse cycle — this is the "
        "discipline the pin table enforces while a transfer is in flight."
    ),
}

# ---------------------------------------------------------------------------
# Emit JSON
# ---------------------------------------------------------------------------
result = {
    "source_parameters": {
        "rtl/cefe_addr_mapper.sv": {
            "LOGICAL_ID_W": LOGICAL_ID_W, "HOT_ENTRIES": HOT_ENTRIES,
            "BACKING_ENTRIES": BACKING_ENTRIES, "PTR_W": PTR_W,
            "derived_HOT_IDX_W": HOT_IDX_W, "derived_BK_IDX_W": BK_IDX_W,
            "derived_TAG_W": TAG_W,
        },
        "rtl/cefe_pin_table.sv": {
            "NUM_ENTRIES": pin["NUM_ENTRIES"], "TENANT_W": pin["TENANT_W"],
            "CHUNK_W": pin["CHUNK_W"], "GEN_W": GEN_W,
        },
        "deployment": {"tokens_per_context": TOKENS_PER_CONTEXT,
                       "chunk_tokens": CHUNK_TOKENS, "hosts": HOSTS},
        "budget_KiB": BUDGET_KIB,
    },
    "object_universe": {
        "formula": "(1M tokens / 64-token chunks) x 16 hosts",
        "chunks_per_host": chunks_per_host,
        "objects": universe,
        "object_id_bits": OBJ_ID_W,
    },
    "naive_flat_directory": naive,
    "two_tier_mapper": two_tier,
    "resident_set_directory": resident,
    "aliasing": aliasing,
}

out_path = OUT / "directory_sizing.json"
out_path.write_text(json.dumps(result, indent=2))

# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------
print(f"Universe: {universe:,} objects ({chunks_per_host:,} chunks/host x {HOSTS} hosts), "
      f"id width {OBJ_ID_W} b")
print(f"Budget: {BUDGET_KIB} KiB = {BUDGET_BITS:,} bits")
print()
print("Naive flat directory (full universe):")
for k, v in naive.items():
    print(f"  {k:38s} {v['total_KiB']:8.1f} KiB  deficit {v['deficit_KiB']:8.1f} KiB")
print()
print("Two-tier mapper (as implemented): "
      f"{KiB(mapper_bits):.2f} KiB -> fits, but covers {mapper_universe:,} ids; "
      f"at {universe:,} objects Tier-2 folds {universe // BACKING_ENTRIES}:1 tagless")
print("Two-tier scaled to universe:      "
      f"{KiB(scaled_bits):.2f} KiB -> DOES NOT FIT (gap "
      f"{KiB(scaled_bits - BUDGET_BITS):.1f} KiB; min budget ~{KiB(scaled_bits):.0f} KiB)")
print()
print("Resident-set directory {valid, id18, gen16, slotptr}:")
for s, v in pool_sweep.items():
    print(f"  pool {s:>6s} slots: {v['entry_bits']} b/entry -> "
          f"{v['total_KiB']:8.2f} KiB  fits={v['fits_budget']}")
print(f"  crossover: {max_fit:,} slots "
      f"(largest power of two: {largest_pow2:,} at "
      f"{KiB(largest_pow2 * resident_entry_bits(largest_pow2)):.1f} KiB)")
print()
print("Aliasing:")
print(f"  as-designed (4096 ids, 3b tag): {pairs_design:,.1f} alias pairs; "
      f"P(alias|lookup, 400 active) = {p_lookup_active:.3f}")
print(f"  scaled (262,144 objs, 9b tag): {pairs_scaled:,.1f} alias pairs; "
      f"P(alias|lookup) = {p_lookup_scaled:.3f}")
print(f"  false-admit after 16b gen check: {hazard_pairs_scaled:.2f} expected "
      f"hazardous pairs (design universe: {hazard_pairs_design:.4f}); "
      f"P(false admit|lookup) = {p_false_admit_lookup:.2e}")
print()
print(f"Wrote {out_path}")
