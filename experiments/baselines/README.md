# Seven-mechanism baseline comparison

Fair, paired comparison of seven object-protection mechanisms for CXL KV-cache
promotion, under **one shared logical event trajectory** per `(workload, seed)`.
Produces the single-column 1×2 figure `figures/fig_baseline_summary.pdf`.

```
NoCheck        SharedRef (TraCT-style refcount)   TwoPhase (reserve-then-pull)
GenOnly        GenOnlyEpochFence (EBR grace)      RDMAKey (generation capability)
Segmented/cancelable DMA                          PROSE
(NoCheck is the normalization reference only, not a candidate)
```

## Where each transition lives in the code

The comparison is intentionally driven by **one shared execution path**,
`baseline_common.replay_run` → `_replay_one` → `_do_transfer`. A mechanism is a
declarative `MechanismSpec` (in its own file); the engine consults it at exactly
four decision points and nowhere else, so no mechanism gets a private fast path.

| Transition | Location |
|---|---|
| Descriptor enqueue (host) | `_replay_one`, stage 1 (`descriptor_enqueue_ns`) |
| Protection acquire | `_replay_one`, stage 2 (refcount/reserve) and stage 3 (PROSE fused pin) |
| Endpoint admission / dequeue check | `_replay_one`, stage 3 (`endpoint_admission_ns`) |
| Queue-time eviction / slot reuse | `_replay_one`, stage 4 (`ObjectTable.evict_and_reuse`) |
| Payload issue (segmented or whole) | `_do_transfer` |
| Transfer-time eviction / slot reuse | `_do_transfer`, at the byte offset crossing |
| Per-segment re-validation + cancel | `_do_transfer` (segmented only) |
| Protection release / completion | `_release_protection` |
| Object metadata: epoch, slot, slot_key, pin | `ObjectTable` |
| Throughput / stale / overhead stats | `_summarize`, `valid_throughput`, `stale_mib_per_gib`, `control_header_overhead_pct` |

Hardware timing (link serialization, DRAM row-miss setup, CXL.mem proto/bridge)
is inherited from the repository's calibrated `CXLQueueConfig` constants — this
harness does **not** re-invent link physics; it adds only the per-request
lifecycle replay the per-decode-step simulator cannot express.

## Per-mechanism state machine

For each: **what state transition it hooks**, **where it checks**, **when it
acquires and releases protection**.

### NoCheck / Unsafe (reference only)
- **Check:** none. **Protection:** none.
- Host enqueues → endpoint issues payload directly. A queue-time or
  transfer-time eviction leaves the descriptor on a stale generation and the
  payload is emitted anyway → stale bytes. Defines
  `normalized_throughput(NoCheck) = 1.0`.

### SharedRef (TraCT-style shared refcount)
- **Acquire:** host atomic `increment` on shared object metadata **before**
  enqueue (non-coherent path → flush + atomic + visibility latency
  `refcount_op_latency_ns`, one cacheline `refcount_op_bytes`).
- **Check:** the endpoint may evict only `refcount == 0` objects — so the pin
  itself is the guard; no epoch compare needed.
- **Release:** host `decrement` after payload completion/abort.
- **Span:** enqueue → completion (covers scheduling + queue + transfer).
  ⇒ zero stale, widest Pin/xfer, **Q-reclaim = N**.
- **Coordination:** the increment on the non-coherent metadata path is one
  serialized flush + atomic + visibility exchange the endpoint must observe
  before honoring the eviction veto ⇒ **+1 RTT** (`extra_rtt = 1`), charged at
  `refcount_op_latency_ns` (≪ a full reserve RTT, but not zero). A truly
  cache-coherent metadata region could make this 0; we do **not** assume that.
  The exact assumption is restated in `results/baselines/audit_report.txt`.

### TwoPhase (reserve-then-pull endpoint reservation)
- **Phase 1** `RESERVE(object_id, epoch)`: endpoint atomically checks residency
  + epoch, installs a reservation pin, returns a unique token. Reserve response
  must return before any payload ⇒ **+1 RTT** on the critical path.
- **Phase 2** `TRANSFER(token, dst, len)`; endpoint releases on completion.
- **Span:** reserve-accept → completion. Token is bound to the slot
  incarnation, so a slot reused after the reserve invalidates the token (no
  stale payload). ⇒ zero stale, **Q-reclaim = N**, `extra_rtt = 1`.

### GenOnly (generation check without pin)
- **Check:** at dequeue, compare `current_epoch == expected_epoch` and
  residency. Resident+match ⇒ admit; else reject.
- **Protection:** none — the check does **not** pin, so eviction/slot-reuse may
  fire during the (non-preemptible) transfer.
- Queue-time race → the descriptor is stale at admission → **reject** (no
  payload). Transfer-time race → post-eviction bytes are **stale**. ⇒ proves
  admission validation alone is insufficient. **Q-reclaim = Y**.

### GenOnlyEpochFence (GenOnly + epoch fence, Tigon-style EBR)
- **Check:** identical to GenOnly (one-time epoch compare at dequeue, no pin,
  no transfer-span hold).
- **Reclaim path:** when the placement authority reclaims a slot, the *unlink*
  (epoch bump + slot-key rotation — what a fresh admission check observes)
  takes effect immediately, so a descriptor checked after the request rejects
  exactly as under GenOnly; the *slot overwrite* — the moment an
  already-admitted transfer's payload reads turn stale — takes effect only
  after one grace period has elapsed since the request.
- **Grace period:** one allocator epoch, `eviction_interval_ns` (500 ns
  nominal / 250 ns race-stress) — the workload's natural reclamation
  timescale, the epoch of the reclamation protocol itself. A fence of one
  decode step (1 ms) or one transfer window (16.4 µs for 64 KiB @ 4 GB/s)
  would cover every raced transfer end-to-end and trivially zero the exposure
  (measured: 0 stale) — that measures the fence length, not the mechanism, so
  it is deliberately NOT the reported regime (see
  `run_epochfence_sweep.py` / Test 8 boundary case).
- **Result:** the fence can only shrink the exposure (≤ GenOnly at every
  paired point) — a descriptor that dequeued before the fence expires still
  runs unprotected after it expires, so the post-fence tail of a raced
  transfer is stale. Deferred reclaim is not a substitute for a transfer-span
  pin. **Q-reclaim = Y**.

### RDMAKey (RDMA-style generation capability)
- Each reusable slot carries a `slot_key`, rotated immediately on slot reuse.
- **Check:** the endpoint compares the descriptor's expected key **once**, when
  the payload engine accepts it. No object-level pin.
- Same failure mode as GenOnly for long transfers (one check cannot span the
  transfer); rejects not-yet-started stale descriptors. Checked **once** by
  design, to stay distinct from Segmented. **Q-reclaim = Y**.

### Segmented / cancelable DMA (64 B, 256 B, 4 KiB, 16 KiB)
- Built on the RDMA-style key; payload split into segments.
- **Check:** re-check the slot key before committing each **new** segment
  (`segment_check_latency_ns`). On failure: issue no further segment; segments
  already in the irrevocable pipeline (≤ `max_inflight_segments`) still
  complete; record an abort.
- **Protection:** none spanning the object. Every issued segment pays
  `per_segment_header_bytes` + `per_segment_descriptor_bytes`.
- **Bounded waste:** `stale ≤ segment_bytes × max_inflight_segments`
  (Test 5). Smaller segment ⇒ less waste, more control/header overhead.
  **Q-reclaim = Y**.

### PROSE (fused atomic admission)
- **Check + acquire (one linearization point):**
  `if resident && current_epoch == expected_epoch: pin_count += 1; admit
  else: reject`. No eviction can interleave between the check and the pin.
- **Protection:** eviction may never pick `pin_count > 0`; pin held from
  admission to the last payload beat (or abort).
- Descriptor is **not** pinned while queued ⇒ endpoint keeps queue-time
  autonomous reclamation; a stale queued descriptor is rejected (no payload).
  ⇒ zero stale, `extra_rtt = 0`, Pin/xfer ≈ 1 (transfer-only),
  **Q-reclaim = Y**.
- **Runtime invariant** (checked at every payload issue, Test 6):
  `PAYLOAD_ISSUE(d) ⇒ resident ∧ epoch_match ∧ pin_count > 0`.

## Fairness

- **One trajectory per `(workload, seed)`.** All RNG lives in
  `generate_trace`: promotion arrivals, object choice, host id, and the race
  annotations (`race_queue`, `race_xfer`, `race_frac`). `replay_run` is fully
  deterministic given the trace.
- **Every mechanism replays the identical trace** — same request-arrival order,
  same scheduling, same **eviction *attempts***, same transfer lengths. Races are
  *relative* annotations (a fraction of the queue/transfer window) so the
  logical eviction attempt lands at the same point regardless of a mechanism's
  own admit-latency.
  Crucially this is **not** "identical eviction *decisions*": every mechanism is
  offered the same eviction attempts, but whether an attempt *fires* or is
  *blocked* is the mechanism's own choice (a queue-time pin blocks it; PROSE
  lets it fire and rejects the stale descriptor at admission). That fired-vs-
  blocked split is exactly the retention effect that lets RefCnt/2Phase post
  higher valid throughput — quantified per pair in
  `results/baselines/audit_report.txt` (identical attempt counts, differing
  fired/blocked). This is why RefCnt/2Phase are *non-dominated* in the 2-D
  (throughput, stale) projection while PROSE wins on the other three axes in
  panel (b); the figure is **not** labeled a Pareto frontier.
- **≥ 5 seeds** (10 used by default). Workload set = `nominal`, `large_object`,
  and an explicitly named `race_stress` (occupancy ≥ 90 %, near-saturation
  queueing, reuse interval on the order of a large-object transfer, guaranteeing
  admission→eviction→completion windows). Identical config across all methods.
- No per-method tuning: byte/latency costs live in `configs/baseline_sweep.yaml`
  with inline provenance, never hard-coded in the plotting script.

## Metric definitions (unified)

- `valid_throughput = total_valid_payload_bytes / makespan` (successful bytes
  only); reported normalized to **Unsafe** (the `NoCheck` reference) per
  `(workload, seed)`, reduced by **geometric mean** across pairs. Because the
  Unsafe design wastes link bandwidth on stale payload, a correct mechanism's
  valid goodput can legitimately exceed 1.0.
- `stale_MiB_per_GiB = total_stale_bytes / total_requested_bytes × 1024`
  (byte-weighted global ratio; reject bytes are **not** stale — no payload was
  emitted).
- `pin_span_ratio = (protection_release − protection_acquire) /
  (last_payload_complete − first_payload_issue)`; 0 for pin-less methods.
- `control_header_overhead_pct = (control + header) / wire × 100`.
- `queue_reclaim` ∈ {Y, N}: may the endpoint autonomously evict the object
  while the descriptor is queued but not yet admitted?

## Reproduce

```bash
# 1. run the paired sweep (330 runs: 3 workloads × 10 seeds × 11 mechanisms)
python experiments/baselines/run_baseline_sweep.py

# 1b. epoch-fence variant: same sweep into NEW files (nothing committed is
#     overwritten) + aggregate + reproduction/sanity checks
python experiments/baselines/run_epochfence_sweep.py

# 2. aggregate (geomean + paired bootstrap CI + byte-weighted stale)
python experiments/baselines/aggregate_baselines.py

# 3. fairness / measurement-口径 audit (identical offered + eviction-attempt
#    trace, stale excluded from valid tp, fixed termination, paired norm,
#    RefCnt coherence assumption) — exits non-zero on any violation
python experiments/baselines/audit_baselines.py

# 4. correctness tests (Tests 1–7)
python -m pytest experiments/baselines/tests/test_baseline_correctness.py -q

# 5. compose the figure (runs pre-plot assertions + PDF bbox/font check)
python experiments/baselines/plot_baseline_summary.py

# 6. auto-generated text summary
python experiments/baselines/figure_summary.py
```

## Outputs

```
results/baselines/raw/<workload>_seed<seed>_<method>.jsonl   request-level records
results/baselines/summary_by_run.csv                          run-level
results/baselines/summary_aggregate.csv                       paper-level (figure source)
results/baselines/manifest.json                               provenance + paired-sample count
results/baselines/audit_report.txt                            fairness / measurement audit
results/baselines/figure_summary.txt                          auto text summary
results/baselines_epochfence/                                 epoch-fence rerun (11 methods, new files)
results/baselines/summary_aggregate_with_epochfence.csv       paper-level incl. GenOnlyEpochFence
results/design_space_epochfence.json                          design-space GENONLY_EF rows
figures/fig_baseline_summary.{pdf,svg,png}                    PDF = paper version
```
