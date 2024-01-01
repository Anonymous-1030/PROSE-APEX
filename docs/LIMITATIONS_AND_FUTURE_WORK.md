# PROSE-APEX: Known Limitations and Future-Work Sketches

This document records the gaps between the paper's full contract and the
released artifact, together with concrete design sketches for closing them.
It is intended as both an honest reproducibility note and a roadmap.

---

## 1. Per-Transfer Pin Table in RTL  — **IMPLEMENTED**

**Paper claim (§III-B, Invariant 1):** For every admitted descriptor
`d = <id, g, slot, len>`, during `ISSUE(d) <= t < COMPLETE(d)` the endpoint
must guarantee `MAP[id] = <slot, g>` and `PIN(id, g) > 0`, and reclaim/overwrite
of a slot is legal only when its pin count is zero.

**Release status (implemented in this release):** `rtl/cefe_pin_table.sv` is a
400-entry per-transfer pin table (one in-flight batch per tenant, 16 tenants x
25 admits), wired into `rtl/APEX_PIPELINE.sv`:

1. The OAT allocates a pin on the S2b validation edge (`pcm_pass & ~pipe_stall`)
   — the single linearization point coupling generation validation and pin
   acquisition (Lemma 2).
2. A validated descriptor that cannot acquire a pin (`pin_reject`) is rejected
   on the same 4-cycle bypass as a PCM reject and issues no payload.
3. `RELEASE(d)` fires at the heap verdict (1:1 with allocation), decrementing
   the pin.
4. The object directory's reclaim probe (`reclaim_allowed`, exposed as a
   top-level port) returns 0 while any in-flight transfer pins `(chunk, gen)`,
   so a slot cannot be reused under an in-flight transfer.

**Verification:** `rtl/cefe_pin_table_tb.sv` checks Invariant 1 directly
(reclaim forbidden while pinned, generation-exact, allowed after RELEASE, full
table → alloc_ok=0). The integrated pipeline TB adds Test 9 (in-flight reclaim
blocked). The trace-driven cross-check (`run_rtl_xcheck.py`) still reports **0
mismatches** and the 9/4-cycle latency is unchanged, so the pin table is off the
admission-critical path. Run `make -C rtl pin_check` and `make -C rtl sim`.

**Residual:** The pin currently releases at the heap verdict (adequate for the
short single-extent KV transfer). A DMA-drain-scoped release for long transfers
is provided by `cefe_dma_engine.sv` (item 5); wiring the engine's `xfer_done`
to the pin RELEASE for the push path is the remaining integration step.

---

## 2. Long-Context Hot-Set Cache / Tier-2 Backing SRAM — **IMPLEMENTED**

**Paper claim (§III-C.c):** A 512-entry hot-set cache covers the 200-400 active
chunks per step. Contexts past 128K tokens exceed it, so an optional Tier-2
backing table absorbs overflow under the same causal write discipline.

**Release status (implemented in this release):** `rtl/cefe_addr_mapper.sv`
provides the two-tier logical-chunk → backing-pointer translation:

- **Tier-1** 512-entry, tag-validated, single-cycle hit (multiplicative hash;
  a hash alias returns MISS, not a wrong pointer — the "tag-validated zero
  fallback").
- **Tier-2** 2048-entry backing table, 3-cycle pipelined probe (3 ns at 1 GHz,
  vs. the 100 µs decode step), probed on a Tier-1 miss.
- **Causal write discipline:** mapping installs commit only at `step_commit`
  (decode-step boundary); same-step reads see the previous step's mapping.

**Verification:** `rtl/cefe_addr_mapper_tb.sv` checks single-cycle hit, causal
discipline (uncommitted install invisible), the 3-cycle Tier-2 probe on a
deterministic hash collision, and the tag-validated MISS. Run
`make -C rtl mapper_check`.

**Residual:** The mapper is a standalone module; `NUM_CHUNKS = 512` in
`APEX_PKG.sv` still bounds the *residency bitmap* in `APEX_PCM.sv`. Integrating
the mapper's `lookup_ptr` as the PCM residency index (so the pipeline addresses
>512 logical chunks) is the remaining wiring step. The Python long-context model
(`run_s3_long_context.py`) remains the end-to-end degradation-curve reference.

---

## 3. Formal Mechanization of the Safety Contract — **MODEL-CHECKED**

**Paper claim (§II-C):** Invariant 1, Lemma 1, Lemma 2, and Theorem 1 are stated
and proved in prose.

**Release status (implemented in this release):** The OAT contract is now
machine-checked, two ways:

- `formal/prose_oat.tla` + `formal/prose_oat.cfg` — a TLA+ spec with a runnable
  TLC configuration. The data mover (`IssuePayload`) deliberately does NOT
  re-validate the binding, so the checked invariant `InvZeroRPE`
  (`StalePayload = {}`) holds only because the pin discipline blocks any
  `Reclaim` that would invalidate an in-flight binding — this is the machine
  form of Theorem 1 / property C1. `InvTransferBinding` encodes Invariant 1.
  Run with TLC (`tlc2 -config prose_oat.cfg prose_oat.tla`) or the TLA+ Toolbox.
- `formal/check_oat_model.py` — a Java-free exhaustive BFS checker mirroring the
  same finite instance and invariants, run by `pytest tests/test_oat_model.py`.
  It also proves the check is non-vacuous: with `--break` (pin guard removed)
  it produces a reachable stale-payload counterexample.

**Residual:** The model covers the single-extent KV path; the edge-case state
machines (item 4) are not yet folded into the TLA+ spec. No Coq/Lean port.

---

## 4. Edge-Case State Machines

**Paper acknowledgment (§II-C, §III-B.b):** Generation wraparound, post-reset
versions, descriptor replay, duplicate/aborted completions, and multi-extent
rollback "require the additional coverage stated in the supplementary
reclaim-serialization analysis and are treated as limitations where not
verified."

**Release status:** No supplementary reclaim-serialization analysis exists in
the repository. The code handles the single-extent KV path only.

**Mitigation added in this release:** A Python state-machine sketch
(`formal/edge_case_states.py`) enumerates the missing cases and provides a
reference test scaffold for generation wraparound, reset recovery, descriptor
replay, and multi-extent rollback.

**Roadmap:**
1. Formalize the state machine in TLA+ alongside `prose_oat.tla`.
2. Implement the wraparound and reset-recovery logic in
   `experiments/baselines/baseline_common.py` first, then port to RTL.
3. Add directed tests for each edge case to `tests/test_runtime_staleness.py`.

---

## 5. Mode A Endpoint DMA Engine — **IMPLEMENTED**

**Paper claim (§III-D, Table I):** Mode A uses endpoint-local DMA with P2P
posted writes to a GPU BAR.

**Release status (implemented in this release):** `rtl/cefe_dma_engine.sv`
provides the Mode A push data path:

- PCIe/CXL posted-write TLP formatting (streams `admit_beats` flits to the GPU
  BAR; posted → no completion TLP, matching the non-preemptive/irreversible
  payload path);
- PASID/IOMMU tag insertion on every TLP (`tlp_pasid`) for GPU-directed P2P
  isolation;
- credit-based flow control (`credit_avail` stalls beats without dropping any);
- completion tracking tied to pin release: `xfer_done` pulses with `(chunk, gen)`
  when the last beat is accepted, scoping the pin to `[ISSUE, COMPLETE)`
  (Theorem 1(c)).

**Verification:** `rtl/cefe_dma_engine_tb.sv` checks posted-write streaming,
PASID tagging, credit-based stall/resume, and completion→RELEASE timing. Run
`make -C rtl dma_check`.

**Residual:** The engine is a standalone module; wiring `admit_valid`/`xfer_done`
between `APEX_PIPELINE` (S7) and this engine — so the push path's pin releases at
DMA drain rather than at the heap verdict — is the remaining integration step.
Timing/area of the TLP formatter is a separate synthesis task; the Mode A
throughput ratios (3.1×/5.9×) remain SimCXL projections (see
`docs/RESULT_ALIGNMENT.md` §4).

---

## 6. RPE Measurement Model — **REPLACED (heuristic → mechanistic)**

**Paper claim (§IV-B, Table III):** Unmitigated RPEpayload of 11.2–14.4% across
public traces; gated result 0.

**Release status (fixed in this release):** The earlier sweeps
(`trace_adapter/rpe_sweep.py`, `rpe_fast.py`) computed RPE from a **tuned
probability** `min(0.35, (tenants-1)·k·util)` — a heuristic, not a measurement
of the object-lifetime binding gap. They are now marked **DEPRECATED — DO NOT
CITE** and `results/rpe_sweep.json` should not be used.

The authoritative model is `trace_adapter/rpe_binding_model.py` (driven by
`run_rpe_binding_sweep.py`), which implements the paper's Definition 1 directly:
a descriptor records `(frame, chunk, generation)` at snapshot time, issues after
a per-descriptor queue residence, and is RPE iff its frame was reused before it
issued. Nothing is tuned. It reproduces **11–14% at heavy oversubscription**
(0.5× buffer) and **~2% at 1×**, reconciling the paper's headline band with the
repo's own nominal rebuttal figure. See `docs/RESULT_ALIGNMENT.md` §1 (which also
records the "Splitwise Conv" → "Azure-Conv" trace substitution).

**Residual:** ARC/LIRS/SIEVE nuance in the binding model uses a common
recency-order victim; the eviction *policy* affects which chunk is reused, not
the binding-gap mechanism being measured. Table V / Table VI accuracy numbers
remain modeled (no LLM inference); see RESULT_ALIGNMENT.md §§2–4.

---

## 7. RTL Cross-Check Decision Consistency

**Release status (fixed in this release):** `experiments/run_rtl_xcheck.py` now
compares the RTL admission decision for every descriptor against an independent
Python reference model that implements PCM validation, Hedge-weighted scoring,
and an exact top-K heap. The README claim of "100% verdict agreement" is now
accurately supported.

---

## Summary Table

| Gap | Severity | Status in this release |
|-----|----------|------------------------|
| RTL pin table / `RELEASE(d)` | High | **Implemented** (`cefe_pin_table.sv`, wired into pipeline; TB + xcheck pass) |
| Hot-set cache / Tier-2 SRAM | High | **Implemented** (`cefe_addr_mapper.sv`; standalone, TB passes; pipeline wiring residual) |
| Formal mechanized proofs | High | **Model-checked** (`prose_oat.tla` + `.cfg` runnable in TLC; Java-free BFS checker in pytest) |
| Mode A DMA engine | Medium | **Implemented** (`cefe_dma_engine.sv`; standalone, TB passes; pipeline wiring residual) |
| RPE measurement model | High | **Replaced** heuristic → mechanistic binding model; heuristics deprecated |
| Edge-case state machines | Medium | Python stub (`edge_case_states.py`); not in TLA+/RTL yet |
| Table V / VI accuracy (no LLM) | Medium | Modeled, not from real inference — documented in RESULT_ALIGNMENT.md |
| "Splitwise" trace substitution | Medium | Azure-Conv stands in; documented in RESULT_ALIGNMENT.md |
| ARC/LIRS/SIEVE + 2x/4x capacities | Medium | **Implemented** |
| RTL per-descriptor verdict agreement | Low | **Implemented** |
