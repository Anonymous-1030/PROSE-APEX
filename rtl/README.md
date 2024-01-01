# CEFE / PPU-APEX Admission-Scoring RTL

Synthesizable SystemVerilog for the **PPU-APEX** scoring datapath and the
**CEFE** admission front-end of PROSE-APEX — the CXL-endpoint logic that binds
an accept/reject verdict *before* a descriptor enters the non-preemptive
CXL.mem payload path.

This is the hardware the paper cross-checks the SimCXL model against: the RTL
closes an **admit in 9 cycles** and a **reject in 4 cycles** at 1 GHz, and
(setting aside the shared MMIO dequeue stage) matches the 8/3-cycle SimCXL
model to within one cycle.

> **Terminology mapping.** The paper's *Object Admission Transaction (OAT)* is
> implemented as the **CEFE/PCM** front-end in this release: the atomic
> check-and-hold maps to `APEX_PCM.sv` (epoch/namespace/residency validation) and
> the reject-bypass path in `APEX_PIPELINE_CTRL.sv`.

> **Scope.** This release ships only the APEX datapath that the paper claims.
> The legacy PHT and QFC engines from earlier PROSE work are *not* part of
> PROSE-APEX and are intentionally excluded.

## Pipeline

```
Descriptor --> [S1: dequeue] --> [S2: PCM validation, 2 cyc] --+--> [S3: expert-bank read]
                                                                |          |
                                            REJECT  <-----------+    [S4: MAC score]
                                          (4-cycle null-complete)          |
                                                                     [S5: top-K heap, 2 cyc]
                                                                           |
                                                                     [S6: weight update (off path)]
                                                                           |
                                                                     [S7: DMA issue]
                                                                     ADMIT (9 cycles)
```

| Stage | Module | Function |
|-------|--------|----------|
| S1 | `APEX_PIPELINE` | Descriptor dequeue from the command ring (MMIO) |
| S2 | `APEX_PCM` | **Payload Commitment Mechanism** — epoch / namespace / residency validation; reject bypass exits here |
| S3 | `APEX_EXPERT_BANK` | Parallel expert-prediction banks (committed-feedback state) |
| S4 | `APEX_MAC_ARRAY` | Weighted accumulation of expert predictions (fixed-point) |
| S5 | `APEX_TOPK_HEAP` | Streaming min-heap top-K admission (K = 25) |
| S6 | `APEX_WEIGHT_UPDATE` | Hedge multiplicative-weight update (off the admission path) |
| S7 | `APEX_PIPELINE` | DMA issue (admit) or null completion (reject) |
| — | `APEX_PIPELINE_CTRL` | Pipeline control + 4-cycle reject bypass |
| — | `ICG` | Integrated clock-gating cell (shared) |

`APEX_PIPELINE.sv` is the top level; `APEX_PIPELINE_TB.sv` is the testbench.

## Simulate (Icarus Verilog)

```bash
make sim
```

Expected output:

```text
[TEST 1] Admitted path latency...      PASS: Admitted in 9 cycles
[TEST 2] Reject path latency...        PASS: Rejected in 4 cycles
[TEST 3] Reject path (already resident) PASS: Residency reject in 4 cycles
[TEST 4] Pipeline throughput...        Got 16 completions in 39 cycles
[TEST 5] Backpressure handling...      PASS
[TEST 6] cfg_flush drain protocol...   PASS
[TEST 7] Adversarial burst...          PASS
=== Results: 8 PASS, 0 FAIL ===
```

> Icarus Verilog prints `sorry: constant selects in always_* processes ...`
> while elaborating `APEX_WEIGHT_UPDATE.sv`. This is an iverilog limitation, not
> a design error — the simulation runs to completion and all five tests pass.
> Commercial elaborators (Genus, DC, VCS, Verilator) handle the construct.

## Synthesize (Cadence Genus / Synopsys DC)

```bash
make synth        # or: cd synth && genus -files synthesize_apex.tcl
```

Edit `synth/synthesize_apex.tcl` to point `init_lib_search_path` / `library` at
your standard-cell PDK. The paper synthesizes against the **ASAP7 7 nm** library
at **1 GHz**; timing closes with positive slack on all paths, with the critical
path through the MAC array and streaming-heap compare.

Reported silicon cost (paper §III-C): APEX scoring pipeline **0.024 mm²**
(Liberty-estimated, `asic/reports/`); full endpoint **1.069 mm²** / 78 mW at
7 nm is a **projection** (not yet in `asic/reports/`), **~216 KiB** of on-chip
state. See `docs/RESULT_ALIGNMENT.md` §4 for the measured-vs-projected split.

`synth/APEX_PIPELINE.sdc` carries the 1 GHz timing constraints.

## Files

```
rtl/
├── ICG.sv                  Integrated clock-gating cell (shared)
├── APEX_EXPERT_BANK.sv     Expert prediction bank (512 x 16-bit)
├── APEX_PCM.sv             Payload Commitment Mechanism (2-cycle validation)
├── APEX_MAC_ARRAY.sv       7-wide weighted accumulator
├── APEX_TOPK_HEAP.sv       Streaming min-heap (K = 25)
├── APEX_WEIGHT_UPDATE.sv   Hedge weight engine (exp LUT)
├── APEX_PIPELINE_CTRL.sv   Pipeline control + reject bypass
├── APEX_PIPELINE.sv        Top-level 9-stage admission pipeline
├── APEX_PIPELINE_TB.sv     Testbench (latency / throughput verification)
├── Makefile                sim / synth / wave / clean targets
└── synth/
    ├── APEX_PIPELINE.sdc       1 GHz timing constraints
    └── synthesize_apex.tcl     Genus (primary) / DC (fallback) flow
```
