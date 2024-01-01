# PROSE-APEX Architecture (release scope)

This document covers the two artifacts in this release — the **SimCXL
extension** and the **CEFE / PPU-APEX RTL** — and how they map onto the paper's
mechanisms. For the full system argument, see the paper.

## The problem: object identity under slot reuse

Shared CXL memory pools save capacity by reusing the same physical slot for
different objects over time. Long-context LLM inference spills the KV cache to
such a pool and promotes hot chunks back as decoding proceeds. Two paths advance
independently: a control path remaps a slot as it places, evicts, and reuses it,
and a data path asynchronously consumes promotion descriptors built from an
earlier snapshot of that mapping.

The CXL.mem payload path is **non-preemptive**: once a DMA descriptor enters the
endpoint payload queue it has already spent link bandwidth, copy-engine cycles,
and queue slots that the protocol never returns, and the transfer *cannot be
undone*. When a slot is reused between the time a descriptor is built and the
time it issues, the request reaches a valid, coherent address but moves the
**wrong logical object version**. This is **Reclaimed-Payload Exposure (RPE)**:
a semantic object-identity error, not an addressability or coherence error. CXL
supplies addressability and coherence but neither object identity nor
transfer-lifetime commitment, so a slot-reusing pool must maintain the
version-to-slot binding itself. The non-preemptive path does not create the gap
but amplifies its cost, since a wrong transfer cannot be recalled.

The fix is to **validate-and-hold at commit time**: at the final admission
point, atomically confirm the descriptor's generation still maps to the slot and
pin that binding until the transfer completes. A single host with a reservation
before enqueue avoids the race by construction; the endpoint earns its keep
across the trust-domain boundary, where only the pool's own directory holds the
authoritative mapping at issue time, no host sees its neighbours' queues, and no
software coordinator can act inside the few nanoseconds before a transfer turns
irreversible.

## Mechanisms

| Mechanism | Expansion | What it does | In this release |
|-----------|-----------|--------------|-----------------|
| **CEFE** | Causal Endpoint Front-End | Pre-payload admission gate at the CXL endpoint; binds accept/reject *before* the non-preemptive payload path, so a reject moves no payload. Eliminates RPE. | `simcxl_ext` model + `rtl` datapath |
| **PCM** | Payload Commitment Mechanism | Validation-before-visibility: an admitted chunk is exposed to attention only after epoch / namespace / integrity checks pass. | `rtl/APEX_PCM.sv` |
| **CFO** | Coalesced Fan-Out | One physical read of a *declared* shared source, fanned out to each requesting domain; matched on a session-setup handle, never on payload contents. | `run_cfo_overlap.py`, `multi_tenant.py` |
| **PPU-APEX** | — | The scoring datapath: expert-bank read, fixed-point MAC, streaming top-K, weight update. Sized to the endpoint budget. | `rtl/APEX_*.sv` |
| **BDB / VC-WRR** | Batch Descriptor Block / virtual-channel weighted round-robin | Amortizes the MMIO doorbell across a step's K descriptors and isolates tenants in independent hardware queues. | `descriptor_batching.py`, `multi_tenant.py`, `endpoint_sim.py` |

> **Note on terminology.** Earlier PROSE drafts expanded CEFE as "Copy-Engine
> Front-End" and PCM as "Promotion Coherence Manager" / "Protocol Compliance
> Monitor". The authoritative expansions, used throughout this release, are
> **Causal Endpoint Front-End** and **Payload Commitment Mechanism**.

## The APEX scoring pipeline (RTL)

```
Descriptor --> [S1 dequeue] --> [S2 PCM validate, 2 cyc] --+--> [S3 expert read]
                                                            |        |
                                         REJECT  <----------+   [S4 MAC score]
                                       (5-cycle null-complete, incl. output register to prevent glitches)       |
                                                          [S5 dual-zone top-K, 2 cyc]
                                                            S5a: classify (0.20 ns)
                                                            S5b: execute  (0.45 ns)
                                                                     |
                                                                [S6 weight update (off path)]
                                                                     |
                                                                [S7 DMA issue]  --> ADMIT (9 cyc)
```

The reject path bypasses scoring and null-completes in 4 cycles (incl. output
register); the admit path runs the full pipeline in 9 cycles. Both are
RTL-validated at 1 GHz (see `../rtl`).

> **Terminology mapping.** The paper's *Object Admission Transaction (OAT)* is
> implemented in this release as the **CEFE/PCM** front-end: the OAT's atomic
> check-and-hold maps to the PCM validation stage (`APEX_PCM.sv`) plus the
> reject-bypass path (`APEX_PIPELINE_CTRL.sv`). The per-transfer pin table and
> `RELEASE(d)` decrement described in the paper are modeled in the Python
> baseline comparison harness (`experiments/baselines/`) but are **not yet**
> synthesized as a separate RTL pin table; see the RTL limitation notes below.

> **Duplicate handling.** The paper states that duplicate chunks null-complete
> without a second transfer. In this RTL release duplicates are handled by the
> **CFO (Coalesced Fan-Out) CAM** (`rtl/cefe_cfo_cam.sv`): a hit suppresses the
> second physical DMA read and multicasts the completion, achieving the same
> zero-payload property for the duplicate while sharing one source read. The
> observable behavior is a coalesced completion rather than a literal
> null-completion.

### Dual-zone exact O(1)-depth top-K selection

A conventional min-heap of K=25 entries requires ⌈log₂ 25⌉ = 5 levels of
comparator sift-down per insertion — over 5 ns, far exceeding the per-cycle
budget. We solve this through a **dual-zone exact O(1)-depth design**:

- **Eviction Zone (EZ):** 7 entries organized as a min-heap of depth 2. The
  root (`ez_min = h_score[0]`) is the global minimum of all 25 retained
  entries and the eviction target.
- **Safe Zone (SZ):** 18 entries stored in a flat register array. A
  combinational 17-comparator min-tree (5 levels, 0.30 ns at 7 nm) tracks
  `safe_min`, refreshed each idle cycle.

**Three-branch admission rule** (when all 25 slots are filled):

| Case | Condition | Action | Critical path |
|------|-----------|--------|---------------|
| 0 (reject) | `x ≤ ez_min` | No state change | 0.20 ns (classify only) |
| 1 (EZ-local) | `ez_min < x ≤ safe_min` | Evict `ez_min`; insert `x` at root; 2-level min-sift-down over EZ | 0.45 ns |
| 2 (cross-zone) | `x > safe_min` | Evict `ez_min`; demote `safe_min` into EZ root, sift-down; write `x` into SZ at `safe_min_idx`; recompute `safe_min` | 0.45 ns |

In all admitting cases, the evicted entry is the **true global minimum** —
this is exact top-K, not an approximation (zero recall loss confirmed against
exact-sort oracle across all evaluation traces).

**Hardware cost:** 19 comparators (6 for sift + 17 for min-tree - 4 shared) +
2 registers (`safe_min`, `safe_min_idx`), adding ~15–20% to the top-K module
area (0.003 mm² absolute). The min-tree executes during the idle cycle of the
2-cycle-per-candidate pipeline cadence — no stall under nominal operation.

The weight-update unit retires off the admission path once per decode step, so
it is not on the critical path.

## Causal boundary

Every deployable policy obeys **update-then-predict**: step *t*'s attention
outcome stays isolated while the step runs, commits when it retires, and reaches
the policy no earlier than step *t+1*. Full-attention distributions are offline
diagnostic labels only — they never steer a runtime decision. The RTL feedback
interface (`fb_*` ports on `APEX_PIPELINE`) is asynchronous to the scoring path
for exactly this reason.

## How the two artifacts relate

The `simcxl_ext` Python model and the `rtl` SystemVerilog are two views of the
same endpoint datapath. The paper cross-checks the model against the RTL to
within one cycle:

* Admit      : 9 RTL cycles  vs 8 model cycles  (full pipeline + MMIO completion)
* PCM reject : 4 RTL cycles  vs 3 model cycles  (S2 bypass completion)
* Heap reject: 9 RTL cycles  vs 8 model cycles  (full pipeline, status = reject)

See [SIMCXL_EXTENSION.md](SIMCXL_EXTENSION.md) for the parameter-level mapping
and calibration sources.
