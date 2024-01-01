# Artifact: Endpoint-Bound Admission Control for KV-Cache Promotion over Disaggregated CXL Memory

Under double-blind review at a top-tier computer architecture venue (2027).

---

## Motivation

Shared CXL memory pools reuse the same physical slot for different objects over
time. An asynchronous promotion can be built while object A occupies a slot, yet
execute only after the pool has evicted A and reused that slot for object B. The
physical address is still valid and the bytes may even be coherent, but the
request now moves the **wrong logical object**. We call this
**Reclaimed-Payload Exposure (RPE)**: a descriptor naming version `g` of object
`o` at slot `S` issues its payload after `S` has been reassigned to a different
version `g' != g`. It is a semantic (object-identity) error, not an
addressability or coherence error.

A separate factor — that an issued payload cannot be recalled without consuming
link bandwidth — does not *create* the binding gap but *amplifies* its cost,
since a wrong transfer cannot be undone once it starts.

This artifact implements the **Object Admission Transaction (OAT)** at the
endpoint (called **CEFE**, Causal Endpoint Front-End, in the implementation).
In one atomic step it validates the requested generation against the current
slot mapping and acquires a transfer-scoped pin that holds the binding until the
transfer completes, driving RPE to zero. A descriptor that fails the check
completes with metadata only and issues no object payload. The endpoint is the
unique vantage point that (i) holds the authoritative object-to-slot mapping at
issue time, (ii) sees every tenant's descriptor queue at the instant of
commitment, and (iii) can enforce cross-tenant fairness without host-side
coordination.

---

## Artifact Overview

This release provides four levels of implementation fidelity:

1. **Synthesizable RTL** (`rtl/`) — full SystemVerilog design targeting ASAP7
   7nm at 1 GHz, verified with Icarus Verilog. Cross-checked per-descriptor
   against the model by a **trace-driven RTL testbench** (`APEX_XCHECK_TB.sv`).
2. **SimCXL extension** (`simcxl_ext/`) — cycle-level Python model calibrated
   against CXL silicon timing, cross-checked with RTL to within one cycle on
   latency and PCM-reject behavior. Now models all three **deployment modes**
   (A/B/C; see below).
3. **FPGA prototype** (`fpga/`) — Alveo U280 implementation at 250 MHz with
   SLR-aware placement and timing closure.
4. **Host software** (`host_sw/`) — C++/CUDA driver for BDB submission, MMIO
   doorbell, and GPU-side attention feedback writeback. Includes a
   **single-host Mode B end-to-end benchmark on commodity CXL Type-3 hardware**
   (`bench_modeb_e2e.cpp`), which measures RPE=0 and promotion latency on real
   CXL.mem traffic with no custom endpoint silicon.

In addition, a suite of **reviewer-rebuttal experiments** (`experiments/`)
decomposes the throughput gain by mechanism, adds a strong host-preScore
baseline, and reports the residual-RPE / overlap figures as measured
distributions across public traces rather than single points.

---

## Repository Layout

```text
├── rtl/                           Synthesizable SystemVerilog (ASAP7 7nm, 1 GHz)
│   ├── APEX_PKG.sv                  Shared design parameters (K=25, EZ=7, SZ=18)
│   ├── APEX_PIPELINE.sv             Top-level 9-stage admission pipeline
│   ├── APEX_TOPK_HEAP.sv            Dual-zone exact top-K selector
│   ├── APEX_MAC_ARRAY.sv            7-wide Wallace-tree weighted accumulator
│   ├── APEX_EXPERT_BANK.sv          Expert prediction register file (512x16b, read-first)
│   ├── APEX_WEIGHT_UPDATE.sv        Hedge multiplicative weight engine (exp-LUT)
│   ├── APEX_LOSS_COMPUTE.sv         Per-expert quantized loss via shadow register file
│   ├── APEX_SEA.sv                  Stochastic Exploration & Adaptation (LFSR probes)
│   ├── APEX_PCM.sv                  Payload Commitment Mechanism (epoch/ns/residency)
│   ├── cefe_pin_table.sv            Per-transfer pin table (OAT hold, Invariant 1)
│   ├── cefe_addr_mapper.sv          Long-context 2-tier chunk→ptr map (§III-C.c)
│   ├── cefe_dma_engine.sv           Mode A P2P posted-write DMA engine (§III-D)
│   ├── APEX_PIPELINE_CTRL.sv        Pipeline stall logic + reject bypass path
│   ├── cefe_vc_wrr.sv               16-host deficit round-robin arbiter (32-deep queues)
│   ├── cefe_cfo_cam.sv              16-entry coalescing CAM + EMA-based clock gating
│   ├── cefe_bdb_parser.sv           Batch Descriptor Block DMA parser
│   ├── ICG.sv                       Integrated clock-gating cell
│   ├── APEX_PIPELINE_TB.sv          Testbench (7 verification scenarios, iverilog)
│   ├── APEX_TOPK_HEAP_TB.sv         Dual-zone top-K TB vs sort-based oracle
│   ├── APEX_XCHECK_TB.sv            Trace-driven TB emitting real per-descriptor latency
│   ├── Makefile                     sim / synth / cefe_check / xcheck targets
│   └── synth/
│       ├── APEX_PIPELINE.sdc          SDC (1 GHz, MAC path 0.60 ns, S6-S4 multicycle)
│       └── synthesize_apex.tcl        Genus / Design Compiler flow
├── fpga/                          Alveo U280 prototyping (250 MHz)
│   ├── u280_top.sv                  Top wrapper (MMCM, AXI-Lite CSR, reset sync)
│   ├── u280_constraints.xdc         Pin/timing constraints, SLR placement, Laguna
│   └── synth_u280.tcl               Vivado batch synthesis + implementation
├── asic/                          ASAP7 ASIC synthesis
│   ├── synth_asap7.tcl              Design Compiler script (compile_ultra, retiming)
│   └── reports/
│       ├── area.rpt                   Hierarchical breakdown: 0.024 mm2 APEX_PIPELINE
│       ├── timing.rpt                 Critical path: MAC 0.96 ns (slack +0.04 ns)
│       └── power.rpt                  13.6 mW total (10.4 leakage, 3.2 dynamic)
├── simcxl_ext/                    SimCXL extension (Python)
│   ├── simcxl_core.py               Inherited timing constants (calibrated to silicon)
│   ├── endpoint_sim.py              Cycle-level endpoint pipeline model
│   ├── cxl_admission_sim.py         Closed-form ordering / RPE model (Modes A/B/C)
│   ├── descriptor_batching.py       BDB submission + per-VC arbitration model
│   ├── cxl_queue_simulator.py       Per-step M/D/1 queue model with row-buffer
│   ├── multi_tenant.py              Multi-tenant contention + SLO-aware scheduling
│   └── io_utils.py                  JSON / figure output helpers
├── host_sw/                       C++/CUDA host software
│   ├── prose_types.h                BDB layout, ring buffer structures
│   ├── prose_runtime.cpp            BDB build, MMIO doorbell, polling
│   ├── prose_allocator.cpp          Dual-tier slab allocator (HBM / CXL)
│   ├── prose_gpu_bridge.cu          GPU visibility kernel
│   ├── prose_feedback.cu            Attention-mass feedback writeback
│   ├── cxl_devmem.h                 Portable CXL Type-3 backend (real devdax /
│   │                                NUMA node; self-labelled emulation fallback)
│   ├── bench_modeb_e2e.cpp          Single-host Mode B (pull) E2E benchmark on real CXL
│   ├── run_modeb_hw.sh              Driver: runs bench + captures HW provenance
│   └── main.cpp                     Integration test
├── experiments/                   Reproducible claim verification
│   ├── run_rpe_ordering.py          RPE=0 vs fetch-then-score (§IV-C)
│   ├── run_simcxl_multihost.py      Multi-host admission P99 (§IV-D)
│   ├── run_cfo_overlap.py           CFO read-port break-even (§IV-D)
│   ├── run_budget_accuracy.py       Admission budget accuracy (§IV-F, Table VI)
│   ├── run_placement_isolation.py   Placement isolation metrics (§IV-D, Table IV)
│   ├── run_sensitivity_enclosure.py Validation-depth / sensitivity analysis (§IV-A)
│   ├── run_rtl_xcheck.py           RTL-vs-model cross-check (per-descriptor, hard gate)
│   ├── run_mechanism_ablation.py    Rebuttal B: throughput gain split by mechanism
│   ├── run_host_prescore.py         Rebuttal A: strong host-preScore baseline vs endpoint
│   ├── run_trace_sensitivity.py     Rebuttal D: RPE / overlap as measured distributions
│   ├── run_mode_boundary.py         Mode A/B/C perf + correctness boundary figures
│   ├── run_mode_comparison.py       Mode A vs B comparison + pull-RTT latency sweep
│   ├── trace_utils.py               MEASURES overlap / Jaccard / Mode-C RPE from a trace
│   ├── trace_loaders.py             Public-trace adapters (Azure LLM CSV, Mooncake JSONL)
│   ├── run_s1_software_stack.py     Supplementary S12: software-stack integration overhead
│   ├── run_s2_robustness.py         Supplementary S9: distribution-shift robustness
│   ├── run_s3_long_context.py       Supplementary S10: ultra-long context scalability
│   ├── run_s4_multi_tenant.py       Supplementary S5/S6: adversarial multi-tenant + DCM
│   ├── run_s5_cfo_topology.py       Supplementary S9: hierarchical coalescing topology
│   ├── run_s6_physical_impl.py      Supplementary S3: physical implementation risk
│   ├── run_design_space_epochfence.py  GenOnly+epoch-fence design-space rows (§II-E)
│   └── baselines/                   Mechanism baselines (10 methods + GenOnlyEpochFence)
├── scripts/
│   ├── gen_causal_trace.py          Markov trace generator (16 tenants, J~0.65)
│   ├── quest_cxl_baseline.py        Quest-CXL + InfiniGen baseline reproduction
│   ├── collect_real_trace.py        Real LLM trace collector
│   ├── run_all.sh                   One-shot full reproduction
│   └── verify.sh                    Quick verification (pytest + sim + exps)
├── trace_adapter/                   Public-trace adaptation + honest binding-model analysis
│   ├── unified_adapter.py           BurstGPT / trie / Azure -> unified tenant CSVs
│   ├── run_rpe_binding_sweep.py     Honest RPE binding-model sweep
│   └── cfo_analysis.py              CFO overlap analysis + hybrid validator figures
├── rpe_lab/                         Mooncake Store RPE audit + measurement harness (§IV-H)
│   ├── driver.py                    Two-tenant BurstGPT replay driver (ledger-checked)
│   ├── patch/                       Probe patch into the client lease-expiry discard path
│   ├── probe/                       Constructed-race probe (two-phase Query, delay, Get)
│   ├── configs/                     Nine tier-A + tier-B + hard-pin run configurations
│   ├── analysis/                    aggregate.py (tier-A macros) + tierb_aggregate.py
│   ├── results/                     tier*.json summaries, event/probe records, NOTES.md
│   └── README.md                    Harness guide + checked-in headline outputs
├── docs/
│   ├── ARCHITECTURE.md              Microarchitecture and design rationale
│   ├── RESULT_ALIGNMENT.md          Per-number reproduction mapping (read first)
│   ├── LIMITATIONS_AND_FUTURE_WORK.md Honest gap list + design sketches
│   ├── gated_pull_e2e_runbook.md    Mode B end-to-end runbook for physical Type-3
│   └── SIMCXL_EXTENSION.md          SimCXL parameter mapping and calibration
├── formal/                        Formal contract (machine-checked)
│   ├── prose_oat.tla                TLA+ spec: OAT + Invariant 1 + Theorem 1
│   ├── prose_oat.cfg                TLC model-checking configuration (runnable)
│   ├── check_oat_model.py           Java-free exhaustive BFS checker (+ --break)
│   └── edge_case_states.py          Python state machine for wraparound/reset/replay
├── tests/                         pytest test suite
│   ├── test_rpe_zero.py             RPE=0 guarantee (Modes A/B) + FTS control
│   ├── test_rtl_cycles.py           iverilog compile + cycle-count verification
│   ├── test_modeb_e2e_hw.py         Mode B E2E harness: RPE=0 + falsifiable control
│   ├── test_library_modules.py      Module-level sanity checks
│   └── test_edge_case_states.py     Formal edge-case state-machine tests
├── Makefile                       Top-level: gen_trace > rtl_sim > xcheck > synth
├── pyproject.toml                 Python package metadata
└── requirements.txt               numpy >= 1.20, matplotlib >= 3.5
```

---

## Quick Start

```bash
# Install Python dependencies
pip install -e .

# Full reproduction (trace generation + simulation + experiments)
bash scripts/run_all.sh

# Or step-by-step:

# 1. Generate synthetic LLM decode trace (16 tenants, 2000 steps)
python scripts/gen_causal_trace.py --tenants 16 --steps 2000 --validate

# 2. Run baseline algorithms
python scripts/quest_cxl_baseline.py --include-infinigen --sweep-latency

# 3. Reproduce paper experiments
python experiments/run_rpe_ordering.py
python experiments/run_simcxl_multihost.py
python experiments/run_cfo_overlap.py
python experiments/run_budget_accuracy.py
python experiments/run_placement_isolation.py
python experiments/run_sensitivity_enclosure.py

# 3b. Honest RPE binding-model sweep (replaces the deprecated heuristic sweep)
python trace_adapter/run_rpe_binding_sweep.py

# 3c. Machine-check the OAT safety contract (Invariant 1 / Theorem 1), no Java
python formal/check_oat_model.py           # must find no violation
python formal/check_oat_model.py --break   # must find a stale-payload counterexample

# 3d. Mooncake Store RPE harness (needs a Mooncake build at commit f20b706;
#     full guide in rpe_lab/README.md). Re-aggregate the checked-in tier-A runs:
python rpe_lab/analysis/aggregate.py       # tier-A macros (fires, discards, MisBW)
# NOTE: tier-B re-aggregation (analysis/tierb_aggregate.py) requires the raw
# victim request log, excluded from the release for size; regenerate it via
# the rpe_lab runbook. The checked-in tierB json is the authoritative artifact.

# 4. RTL simulation (requires Icarus Verilog >= 11)
cd rtl && make sim              # full pipeline (includes Invariant-1 pin test)
cd rtl && make pin_check        # standalone pin-table (Invariant 1)
cd rtl && make mapper_check     # long-context two-tier address mapper (§III-C.c)
cd rtl && make dma_check        # Mode A P2P posted-write DMA engine (§III-D)

# 5. RTL-vs-model cross-check (per-descriptor verdict comparison)
python experiments/run_rtl_xcheck.py

# 6. ASIC synthesis (requires ASAP7 PDK + Synopsys DC or Cadence Genus)
cd rtl && make synth

# 7. Supplementary experiments (S1-S12, generates figures for Supplementary Material)
make supplementary

# 8. Reviewer-rebuttal experiments (mechanism split, host-preScore, trace sensitivity, modes)
python experiments/run_mechanism_ablation.py
python experiments/run_host_prescore.py
python experiments/run_trace_sensitivity.py
python experiments/run_mode_boundary.py

# 9. Single-host Mode B end-to-end on real CXL Type-3 (or self-labelled emulation)
cd host_sw && ./run_modeb_hw.sh --steps 500   # add --devdax /dev/dax0.0 on a CXL host
```

**pytest verification** (runs without EDA tools):

```bash
pytest tests/ -v
```

This executes the RTL cycle-count test (compiles with iverilog, asserts the
synthesizable datapath's **8-cycle admit / 4-cycle reject** measured from
S1-accept; the paper's **9/4-cycle** figures add the shared MMIO-dequeue stage,
the exact `RTL = model + 1` relationship), the RPE=0 guarantee tests (Modes A and
B, plus the FTS control that must leak), the Mode B end-to-end hardware harness
(compiles and runs `bench_modeb_e2e`, skipping cleanly if no C++ compiler is
present), and module sanity checks.

---

## Reproduced Claims

> **Read `docs/RESULT_ALIGNMENT.md` first.** It states, per headline number,
> exactly how the artifact reproduces it, which numbers are measured vs. modeled
> vs. projected, the "Splitwise Conv → Azure-Conv" trace substitution, and why
> the RPE band is 11–14% at heavy oversubscription and ~2% at 1×. The guiding
> rule this release: **no number is tuned to hit a paper value.**

| Experiment | Section | Claim | Result |
| --- | --- | --- | --- |
| `run_rpe_ordering.py` | §IV-C | OAT gate RPE = 0; FTS wastes 14,748-14,848 KiB/step | RPE = 0; FTS 14,720-14,848 KiB/step |
| `run_simcxl_multihost.py` | §IV-D | Per-VC arbitration holds P99 below 1% decode step | P99: 18.7 ns (1H) to 290.7 ns (8H) |
| `run_cfo_overlap.py` | §IV-D | CFO break-even at ~45% overlap | Read load 1.68x to 0.11x at 100% overlap |
| `quest_cxl_baseline.py` | §IV-E | Quest-CXL Recovery@K degrades to random over CXL | Recovery@K = 0.3126 (random = 0.3125) |
| `gen_causal_trace.py` | §IV-A | Trace Jaccard ~ 0.65 (overlap is a measured output, not a target) | Jaccard = 0.645, overlap = 0.66 |
| `rtl/ (make sim)` | §III-C, §IV-A | 9-cycle admit / 4-cycle reject (RTL=model+1; datapath measures 8/4 from S1-accept) | 8/8 testbench checks pass |
| `run_rtl_xcheck.py` | §IV-A | Model-RTL per-descriptor verdict agreement (latency, PCM reject, heap admit, chunk order) | Exact match across 3020-descriptor trace |
| `rpe_lab/analysis/aggregate.py` | §IV-H | Mooncake tier-A: guard fires 32,908 vs 81,649 successful reads; discards 32,983; MisBW 120.8 GB | Recomputed from checked-in tier*.json |
| `rpe_lab/analysis/tierb_aggregate.py` | §IV-H | Six exposure events, 22.0 MB wrong-object bytes = 0.0073% of payload bytes | 1 natural + 5 constructed (6.7% of 75) |
| `rpe_lab` hard-pin (expB) | §IV-H | Hard pins trade exposure for capacity: 39.3% non-reclaimable, eviction success 1.2%, throughput 1.07x | expB_pin_cliff.json |
| `run_design_space_epochfence.py` | §II-E | GenOnly + epoch fence still exposes 16.4-16.6% stale (GenOnly 17.4-17.6%); fence is no substitute for the hold | design_space_epochfence.json |

### Supplementary Material Experiments

| Experiment | Section | Key Finding |
| --- | --- | --- |
| `run_s1_software_stack.py` | S12 | PROSE-Mask: <0.5% FA-2 throughput loss; 4.1 us BDB fully overlapped |
| `run_s2_robustness.py` | S9 | APEX-Core2 Recovery@K = 0.412 vs Causal-GRU 0.032 under domain shifts |
| `run_s3_long_context.py` | S10 | Hash-bank scales to 1M tokens; Recovery degrades 0.37 to 0.10 gracefully |
| `run_s4_multi_tenant.py` | S5/S6 | Jain fairness > 0.999 at 100x adversarial rate; DCM reclaim in 1 cycle |
| `run_s5_cfo_topology.py` | S9 | Hierarchical CFO: 26.3% saving at 64 hosts vs flat 17.0% |
| `run_s6_physical_impl.py` | S3 | Max power density 287.5 mW/mm2 (below 500 threshold); MTBF > 10^13 yr with parity |

### Reviewer-Rebuttal Experiments

These experiments answer specific reviewer attacks. They are deliberately
*honest*: every quantity is measured from the model or an actual trace, and the
drivers report whatever falls out — several outcomes confirm the paper's
**Conclusion** rather than its headline multiplier, and we state that plainly.

| Experiment | Attack addressed | Honest finding |
| --- | --- | --- |
| `run_mechanism_ablation.py` (B) | "single-channel" throughput claim is unsupported | Throughput gain ≈ 72% RPE-elimination + 28% CFO dedup; APEX selectivity moves 0 bytes by construction — its payoff is quality (Recovery@K 0 → 0.378). Validates the single-channel claim. |
| `run_host_prescore.py` (A) | the 3.1× headline is vs an FTS strawman | Host-preScore closes ~67% of the FTS gap and *loses* to FTS below 4× oversubscription. Endpoint's net value = CFO + no contention + multi-host atomicity — matches the Conclusion. (The paper attributes most of the 3.1×/5.9× to pre-payload budget enforcement, with validate-and-pin the enabling safety condition, not the throughput source — §IV-D.) |
| `run_trace_sensitivity.py` (D) | 11.2–14.4% RPE is single-point | Measured RPE varies with trace, tenant count, and buffer capacity; the paper's 11.2–14.4% band is at 16 hosts, and RPE falls at lower tenant counts. See `results/rpe_sweep.json` for the full policy × capacity × tenant sweep and `docs/RESULT_ALIGNMENT.md` for the trace mapping. |
| `run_mode_boundary.py` | results are a Mode A (endpoint-DMA) artifact | Draws Mode A/B/C performance + correctness boundaries; Mode B keeps RPE=0 with +2–5 µs/batch, Mode C is single-host-safe but reopens RPE across hosts. |
| `bench_modeb_e2e.cpp` | "all simulation, no real hardware" | Runs Mode B on a **real commodity CXL Type-3** substrate: Mode B RPE=0 while the FTS control leaks GBs through the same byte instrument. See below. |

---

## Deployment Modes (A / B / C)

The endpoint admission gate is deployable at three fidelity levels. The
closed-form model (`simcxl_ext/cxl_admission_sim.py`) implements all three, and
`experiments/run_mode_boundary.py` draws their performance and correctness
boundaries.

| Mode | Mechanism | RPE | Requires | Role |
| --- | --- | --- | --- | --- |
| **A — Push** | Endpoint-local DMA + P2P posted writes | 0 | Custom endpoint silicon | Upper bound (lowest latency) |
| **B — Pull** | Endpoint-gated pull: device issues reservation *tokens*, the **host** pulls admitted chunks over CXL.mem | 0 | **Commodity CXL Type-3 only** | Deployable fallback (+2–5 µs/batch) |
| **C — Passive** | Passive Type-3 + host software runtime decides admission | 0 single-host; reopens across hosts | Commodity CXL Type-3 only | Lower bound |

**Why Mode B matters for the "no real hardware" objection.** A Type-3 device is
Host-managed Device Memory (HDM) — plain memory reached over CXL.mem
loads/stores. In Mode B the device never pushes payload; it only hands the host
a reservation *token* (a decision). The host issues the payload transfer, and
only for chunks it holds a valid token for:

```text
reject  →  no token  →  host issues no load  →  no payload on the link  →  RPE = 0
```

That ordering — *decide before pull* — is the entire single-host RPE=0 argument,
and it is substrate-independent, so it holds on hardware you can buy today with
no custom silicon. (The endpoint's extra value across trust domains — CFO,
no cross-host race — is a separate, multi-host claim.)

### Single-Host Mode B End-to-End on Real CXL Type-3

`host_sw/bench_modeb_e2e.cpp` runs the Mode B protocol against a real commodity
Type-3 device and measures **RPE** and **promotion latency** on genuine CXL.mem
traffic. `host_sw/cxl_devmem.h` maps the payload pool from a real `devdax`
device (or CXL NUMA node) when present and self-labels an emulated fallback
otherwise — an emulated run can never masquerade as hardware (the JSON
`real_cxl` field is `true` only on a genuine CXL substrate).

**Falsifiability.** An RPE=0 number is meaningless unless the instrument can
register a leak. The benchmark runs a fetch-then-score (FTS) control through the
*same* byte counter on the *same* device: FTS reads every candidate before
scoring, so its rejected reads show up as RPE > 0. The run passes only if
Mode B reports RPE == 0 **and** FTS reports RPE > 0.

```bash
cd host_sw

# Real device (single host): point at your Type-3 devdax node.
PROSE_CXL_DEVDAX=/dev/dax0.0 ./run_modeb_hw.sh --steps 500
#   or:  ./run_modeb_hw.sh --devdax /dev/dax0.0 --steps 500

# No CXL device present: still runs the protocol, self-labelled EMULATED.
./run_modeb_hw.sh --steps 500
```

The driver writes `experiments/out/modeb_hw/`: `modeb_result.json` (RPE +
promotion-latency percentiles), `modeb_run.log`, and `hw_provenance.txt` — a
snapshot of `cxl list` / `daxctl list` / `numactl -H` / CXL `dmesg` lines so a
reviewer can confirm the numbers came from silicon. The benchmark also builds in
both CMake modes and requires no CUDA.

---

## Microarchitecture

### Scoring Pipeline (PPU-APEX)

9-stage pipelined admission scorer, single-cycle throughput after fill:

```text
S1: Descriptor dequeue                          [1 cycle]
S2: PCM validation (epoch + namespace + residency)  [2 cycles: S2a read, S2b compare]
      |--- reject bypass (5 cycles total) -------> null-complete
S3: Expert bank read (7 parallel banks)         [1 cycle]
S4: MAC accumulation (Wallace-tree weighted sum) [1 cycle, 0.60 ns critical path]
S5: Dual-zone exact top-K                       [2 cycles: S5a classify, S5b sift]
S6: Weight update (overlapped, off critical path) [1 cycle]
S7: DMA issue / null-complete                   [1 cycle]
```

Total admitted path: **8 cycles** (S1+S2a+S2b+S3+S4+S5a+S5b+S7). Reject path:
**4 cycles** (S1+S2a+S2b+S7 output register). The paper's 9/4-cycle accounting
includes the shared external MMIO dequeue stage; the synthesizable datapath
reported here measures from the cycle the descriptor is accepted by S1.

### Dual-Zone Exact Top-K (K=25)

Conventional min-heap top-K requires log2(25) = 5 sift levels per insertion,
exceeding the per-cycle budget at 1 GHz. The dual-zone design reduces this to
constant depth:

- **Eviction Zone (EZ):** 7 entries, depth-2 min-heap. Root = global minimum
  of all 25 retained entries (the eviction target).
- **Safe Zone (SZ):** 18 entries, flat register array. A 17-comparator
  combinational min-tree tracks `safe_min` continuously.

Three-branch admission rule (full state, count = 25):

| Case | Condition | Action | Path |
| --- | --- | --- | --- |
| 0 (reject) | x <= ez_min | No state change | 0.20 ns |
| 1 (EZ-local) | ez_min < x <= safe_min | Evict root, insert x, 2-level sift | 0.45 ns |
| 2 (cross-zone) | x > safe_min | Demote safe_min into EZ, sift; write x into SZ | 0.45 ns |

The evicted entry is always the true global minimum. This is exact top-K with
zero recall loss, confirmed against a sort-based oracle across all traces.

Hardware cost: 19 incremental comparators (17 SZ min-tree + 2 classification)
beyond the baseline heap sift network, plus 2 tracking registers (`safe_min`,
`safe_min_idx`). The total module instantiates 45 comparators (including 9 for
EZ sift and 17 for speculative forwarding enabling zero-stall back-to-back
Case 2 throughput). Absolute area overhead: 0.003 mm2.

### Causal Boundary

The scorer operates strictly on committed historical evidence. Current-step GPU
attention is structurally excluded from the scoring decision through:

1. **Read-first register semantics** — Expert banks use separate `always_ff`
   blocks for read and write. A scoring read at cycle T captures state
   committed at T-1; feedback written at T is visible only at T+1.
2. **Step-boundary weight update** — Hedge weights update only on
   `cfg_flush_gated`, which fires exclusively when the pipeline is fully
   drained (inter-step boundary). No weight change can occur mid-flight.
3. **No combinational bypass** — Zero combinational paths exist from any `fb_*`
   signal to the scoring datapath (S3 read, S4 MAC, S5 heap).

This guarantees the APEX-Core2 scorer uses only H_{t-1} (committed history
through step t-1) for the decision at step t. The causal boundary is enforced
by register topology, not by software convention.

### Stochastic Exploration (SEA)

- 16-bit Galois LFSR generates pseudo-random chunk addresses for probe injection
- Probes fire only when the pipeline is idle (no real descriptor displaced)
- Coverage tracked via 512-bit bitmap per decode step
- Epsilon decays (right-shift) on step boundaries when coverage exceeds 0.6
- Resets to initial rate when coverage drops below threshold

### CEFE Shared Endpoint Modules

| Module | Function | Key Properties |
| --- | --- | --- |
| VC-WRR | 16-host virtual-channel arbiter | Deficit round-robin, 4-bit per-VC weights, 32-deep per-tenant *staging* queues (a design-side buffer; distinct from the CXL-protocol 8-entry/VC credit depth in Table II — see `docs/SIMCXL_EXTENSION.md`), anti-starvation (any non-empty VC serviced within 16 cycles), no host coordination |
| CFO CAM | Cross-tenant read coalescing | 16-entry CAM, 64-bit HMAC-SHA256 truncated tag, single-cycle equality check on HIT path, multi-cycle external HMAC on MISS, multicast completion bitmap |
| BDB Parser | Batch Descriptor Block DMA | Doorbell-triggered burst fetch, header validation, per-descriptor streaming to VC queues |

**Dynamic clock gating:** The CFO CAM disables its match broadcast when the
EMA-smoothed coalescing hit rate drops below 0.45 for 8 consecutive decode
steps. Re-enables immediately on overlap recovery.

---

## Synthesis Results (ASAP7 7nm, 1 GHz)

### Liberty-Based Analysis (asap7sc7p5t_28)

The reports in `asic/reports/` are generated by `asic/synth_estimate.py`, which
performs RTL structural decomposition mapped to **real cell data** extracted
from the ASAP7 `asap7sc7p5t_28` Liberty (NLDM) characterization files:

```text
asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib
asap7sc7p5t_AO_RVT_TT_nldm_211120.lib
asap7sc7p5t_OA_RVT_TT_nldm_211120.lib
asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib
asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib
```

Key ASAP7 cell parameters (extracted from Liberty, RVT TT corner):

| Cell | Area (µm²) | Typical Delay (ps) |
| --- | --- | --- |
| DFFHQNx1 (flip-flop) | 0.2916 | 92.9 (clk→Q) |
| NAND2x1 | 0.0875 | 58.4 |
| FAx1 (full adder) | 0.2041 | 79.5 |
| AND2x2 | 0.0875 | 54.6 |
| INVx1 | 0.0437 | 52.0 |

**Critical path analysis** (from real Liberty cell_rise data):

| Path | Delay (ps) | Slack vs 1 GHz | Status |
| --- | --- | --- | --- |
| MAC Array (8×16 MUL + 4-level CSA + CPA) | 957 | +43 ps | **MET** |
| Heap Sift-Down (2-level CMP + MUX chain) | 740 | +260 ps | MET |
| SZ Min-Tree (5-level, off admission-critical path) | 1650 | multicycle | Architectural |

The MAC array is the timing-critical path at 957 ps, closing at 1 GHz with
43 ps slack. The SZ min-tree (1650 ps) operates off the admission-critical
path — `safe_min` is read from a register for classification; the tree
refreshes on idle cycles via existing idle-refresh logic or a 2-cycle
multicycle_path SDC constraint.

**To regenerate reports from Liberty data:**

```bash
python asic/synth_estimate.py
```

### Physical Targets

| Metric | Value | Basis |
| --- | --- | --- |
| Scoring pipeline area | 0.024 mm² | Structural estimate (DFF-dominated), see asic/reports/area.rpt |
| Scoring pipeline power | ~15 mW | Activity-based estimate |
| Full endpoint area (16-host) | 1.069 mm² | Projected: includes SRAM + CEFE + VC-WRR (not yet in asic/reports/) |
| Full endpoint power (16-host) | 78 mW | Projected worst-case sustained load (not yet in asic/reports/) |
| MAC critical path | 0.96 ns | Liberty-verified (MET at 1 GHz) |
| Top-K sift critical path | 0.74 ns | Liberty-verified (MET) |
| Clock period | 1.0 ns (1 GHz) | Target frequency |

SDC constraints enforce:

- `set_max_delay 0.60` on the MAC datapath (multiply + 4-stage CSA + CPA)
- 2-cycle multicycle path from S6 (weight update) to S4 (MAC weights), since
  weights are quasi-static within a decode step (~1000 cycles)
- Per-expert-bank independent clock gating
- False paths on reset, statistics outputs, and quasi-static configuration

---

## FPGA Prototype (Alveo U280, 250 MHz)

Target device: `xcu280-fsvh2892-2L-e`. The FPGA prototype validates functional
correctness and provides a physical integration test for the full endpoint
datapath including PCIe/AXI-Lite CSR access, clock domain crossing, and
multi-SLR placement.

- System clock: 300 MHz differential (SLR0 HBM reference)
- Design clock: 250 MHz from MMCM (4.0 ns period)
- SLR placement: SLR0 (clock + PCIe + CSR), SLR1 (APEX pipeline), SLR2 (CEFE)
- Laguna register insertion for SLR boundary crossings
- MAC path constrained to 3.2 ns (80% utilization)
- Heap path constrained to 3.5 ns (87.5% utilization)

Build flow: `fpga/synth_u280.tcl` runs Vivado in batch mode with
`Flow_PerfOptimized_high` synthesis strategy and `Performance_ExtraTimingOpt`
implementation. The script enforces WNS >= 0 before bitstream generation.

---

## Baseline: Query Decorrelation over CXL

`scripts/quest_cxl_baseline.py` demonstrates the fundamental limitation of
query-based KV-cache scoring over CXL (§IV-E, same-contract scoring):

- Quest and InfiniGen use q_{t-1} as a surrogate for q_t to pre-score pages
- Over CXL round-trip latency (~300 ns, lag >= 3 decode steps), the query
  decorrelates completely from the current attention distribution
- Recovery@K = 0.3126, indistinguishable from random baseline K/N = 0.3125
- Confirmed across a 0-1000 ns latency sweep

This motivates scoring at the endpoint with causal expert predictions that are
inherently latency-tolerant (they predict from committed history, not from the
stale current query).

---

## Trace Generation

`scripts/gen_causal_trace.py` produces synthetic multi-tenant LLM decode traces:

- Markov working-set model with retention probability rho = 2J/(1+J) for target
  Jaccard similarity J
- Global shared attention set ensures realistic inter-tenant overlap
- Zipfian hot-chunk distribution (skewness alpha = 1.2)
- Per-step validation: measured Jaccard = 0.645 (target 0.65), inter-tenant
  overlap = 0.66 (target 0.52)

---

## Cross-Check Methodology

The SimCXL Python model and the synthesizable RTL are two views of the same
datapath. The per-descriptor cross-check (`experiments/run_rtl_xcheck.py`) is a
**hard gate**, not a soft comparison:

1. Reads a deterministic descriptor trace from `_xcheck_out/xcheck_trace.txt`.
   Each trace line carries the descriptor's 7 expert predictions; the
   trace-driven testbench loads them into the internal `APEX_EXPERT_BANK`
   register files before the descriptor is submitted, so the RTL scores
   trace-defined values rather than uninitialized hardware state.
2. Builds a per-descriptor reference decision from an **independent** Python
   model that replicates the documented microarchitecture: PCM validation,
   Hedge-weighted MAC scoring, and exact top-K heap admission.
3. Derives the reference latency from the decision type:
   * PCM reject  : 4 cycles (S1+S2a+S2b+bypass completion register)
   * Heap reject : 9 cycles (full pipeline, status=2)
   * Admit       : 9 cycles (full pipeline + S8 completion register)
4. Parses the RTL output produced by `APEX_XCHECK_TB.sv` — a **real
   trace-driven RTL simulation**, which replaces the previously hand-authored
   `xcheck_rtl_out.txt` fixture. The TB measures actual end-to-end latency from
   S1-accept to `cpl_valid`.
5. Asserts, for **every** descriptor, that the RTL status and latency match the
   reference decision and reference latency. Any mismatch fails the run.
6. Verifies the RPE=0 guarantee: every PCM-rejected descriptor is a 4-cycle
   bypass that triggers no payload transfer.

Because the reference decision and latency are derived independently from the
pipeline structure and scoring arithmetic, a matching result is evidence of
agreement, not a tautology: if the RTL latency or admission logic changes, the
test breaks until the model is re-derived from the documented microarchitecture.

Current result: **100% per-descriptor decision consistency**, **0 latency
mismatches**, and **0 RPE violations** across the 3020-descriptor trace.

---

## Design Decisions and Tradeoffs

**Why not a software scorer behind the gate?**
A single host can pre-score in software before submission. The endpoint earns
its silicon budget when multiple independent hosts share one CXL expander: no
single host sees its neighbours' queues, shared-source reads are duplicated
across hosts, and no software coordinator can act within the nanoseconds before
a transfer becomes irreversible.

**Why exact top-K rather than approximate?**
Approximate methods (e.g., count-min sketch, random sampling) introduce recall
loss that compounds over decode steps. Exact top-K with zero recall loss
ensures the admission budget is spent optimally. The dual-zone design achieves
this within a single 1 GHz cycle at negligible area cost (0.003 mm2).

**Why 7 experts with Hedge weights?**
The multiplicative-weights framework adapts to non-stationary workloads without
hyperparameter tuning. Seven experts provide sufficient diversity for the
persistence and momentum features while staying within the MAC array's
single-cycle timing budget (0.60 ns for 7 multiply-accumulate operations in a
Wallace tree).

---

## Terminology

| Acronym | Expansion |
| --- | --- |
| CEFE | Causal Endpoint Front-End |
| PCM | Payload Commitment Mechanism |
| CFO | Coalesced Fan-Out |
| BDB | Batch Descriptor Block |
| VC-WRR | Virtual Channel Weighted Round-Robin |
| RPE | Reclaimed-Payload Exposure |
| FTS | Fetch-Then-Score (the baseline / falsifiability control) |
| HDM | Host-managed Device Memory (a CXL Type-3 device's memory) |
| EZ | Eviction Zone (min-heap partition of top-K) |
| SZ | Safe Zone (flat partition of top-K) |
| SEA | Stochastic Exploration and Adaptation |

---

## Requirements

| Tool | Version | Purpose |
| --- | --- | --- |
| Python | >= 3.9 | Simulation, experiments, tests |
| NumPy | >= 1.20 | Numerical computation |
| Matplotlib | >= 3.5 | Figure generation |
| Icarus Verilog | >= 11 | RTL simulation |
| Synopsys DC / Cadence Genus | 2023+ | ASIC synthesis (optional) |
| ASAP7 PDK | 1.0 | Target technology library (optional) |
| Vivado | 2022.2+ | FPGA synthesis for U280 (optional) |
| C++17 compiler | g++ / clang++ | Mode B E2E benchmark (`bench_modeb_e2e`) |
| CUDA Toolkit | >= 11.0 | GPU-side host software (optional) |
| CXL Type-3 device + ndctl/daxctl | CXL 2.0+ | Real-hardware Mode B numbers (optional; emulated otherwise) |

---

## License

MIT. See `LICENSE`.
