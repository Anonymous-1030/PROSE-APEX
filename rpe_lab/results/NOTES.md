# RPE-lab NOTES (decisions, deviations, operational definitions)

Running log. Every deviation from the original plan is recorded here with its
justification and evidence location.

## Deviations from the plan (all confirmed not to affect conclusion validity)

1. **Default TTL is 10000ms, not 5000ms** (types.h:85-86 + master.cpp
   static_assert). Handling: every run passes `--default_kv_lease_ttl`
   explicitly; the TTL sweep still follows the plan {1000, 5000, 11000}.
2. **There are 3 lease check points, all in client_service.cpp**
   (1160/1205/1482); the plan's `transfer_task.cpp` is not involved in leases.
   The instrumentation points match (audit_notes.md Q1).
3. **Seeding folded into the victim process**: the plan's standalone seeder
   process would unmount its segment on exit (losing the object replicas on
   it). Instead the victim (tenant A) seeds and holds its own hot set; the
   pressure tenant waits for the ledger file to appear before putting. The
   pool is unchanged: two tenant processes x 2GB = 4GB.
4. **Python `get()` swallows error codes** (failure returns empty bytes only):
   the driver uses `get_into(key, ptr, size)` everywhere, whose return value is
   byte count on success / negative error code on failure, distinguishing -707
   (guard trip) from -704 (not found).
5. **Expected generation is accounted on the driver side**: the C++ probe
   cannot know the expected gen; events carry only found_* and
   expected_key_hash. The overwritten verdict is computed at aggregation time
   (joining the per-request exp_gen from the request log by key+timestamp).
6. **Join between C++ probe events and the request log**:
   `expected_gen_for(key, ts)` takes the most recent request with
   t_lookup_ns <= ts; falls back to the ledger's final gen.
7. **success_mismatch relaxed to {exp_gen, cur_gen}**: a reseed may
   legitimately complete between lookup and transfer (reading the new gen is
   then "freshest", not an error). The relaxation only covers old/new gens of
   the same key; a foreign key_hash, missing magic, or an old gen still counts
   as mismatch.

## Mechanism findings (audit + smoke measurements; each with file:line or log evidence)

- **Eviction victim = oldest lease_timeout first** (nth_element ascending,
  master_service.cpp:6659-6668); **PutEnd expires the lease immediately**
  (GrantLease(0), master_service.cpp:3305-3308). Implication: never-read
  objects are always the first eviction candidates; frequently read hot keys
  survive on lease renewals.
- **Leases are granted only by GetReplicaList/ExistKey**
  (master_service.cpp:2083-2093 area); Put does not grant a lease. The client
  read path takes no refcnt and registers no task — after lease expiry the
  read side has no fallback at all (F5).
- **memcpy fast path**: auto-enabled in TCP-only environments; only when the
  replica endpoint == this process's endpoint (same-host cross-process traffic
  goes over TCP). The worker pool is **single-threaded with an unbounded
  queue** (transfer_task.cpp:581-625).
- **Replica placement defaults to random** (random start, sequential scan
  across segments; master.cpp:288-290), not local_first. Read replica
  selection takes the first COMPLETE in the list
  (client_service.cpp:3900-3913).
- **The `BatchGetWhenPreferSameNode` path has no lease check** (no
  IsLeaseExpired in client_service.cpp:1221-1366; this experiment's workload
  does not use that path, but it is recorded as an attack-surface observation).
- Smoke measurement: after crossing the 0.5 high watermark, one BatchEvict
  round every ~350ms, ~30 keys/round; old smoke keys all survived on lease
  renewals while new fill keys were evicted (consistent with the F1
  mechanism).
- tc netem works on the WSL2 loopback (both delay and rate verified); the
  memcpy path is unaffected by tc, the TCP path is affected — the two paths
  must be treated separately (Phase 2 chimney measurement: under 400ms tc
  delay, TransferRead took only 408µs via memcpy; the expiry was caused by
  the control-plane RPC round trip).

## Operational definitions (the paper macros' precise meaning)

- **guard_fires**: count of rc=-707 in the victim request log (cross-checked
  against the C++ event count guard_fires_events; the two should be equal —
  if not, the event file wins and the discrepancy is investigated).
- **rpe_events / rpe_payload_bytes**: count and byte sum of discard events with
  found_magic=true and (found_key_hash != fnv1a64(key) or found_gen != that
  request's exp_gen).
- **misbw_bytes**: sum of payload_len over all discard events (bytes fetched
  and then discarded).
- **no_magic_discards**: discards whose buffer had no valid header (slot
  overwritten by headerless content or zeroed).
- **burst_share_pct**: victim arrival rate binned at 10s; bins with rate > 4x
  the whole-run mean are burst windows; the share of discard events falling
  inside burst windows. (The plan's literal "concurrency > 4x pool capacity"
  is unreachable at this experiment's scale; it is operationalized as "4x mean
  arrival rate", as stated in the paper.)
- **throughput_mbps**: victim successful Get bytes / run duration (MB/s,
  decimal MB).

## Trace usage

- File: BurstGPT_1.csv (SHA256 in manifest.md). Only Timestamp (IAT) and Total
  tokens (context length for accounting) are used.
- Windows selected by a full-file density scan: victim `trace_start=1154000`
  (10k requests/577s, mean 17.3/s, per-second peak 91/s); pressure
  `trace_start_b=90000`.
- RPS scaling (trace_speedup) follows the BurstGPT README recommendation
  ("scale the average RPS according to your evaluation setups"); each run's
  config JSON records the value used.

## DoD ladder record (plan section 10 troubleshooting order)

- **DoD run 1** (dod_ttl1000_c64_seed42: TTL=1000, c=64, speedup=10, no rate
  limit, 600s): gets=15798 (ok=10255, -704=5540, -703=3), **guard_fires=0**,
  read latency p50=5.1ms / p90=26ms / p99=62ms / max=243ms << TTL=1000ms.
  Negative confirmation of the window formula: at the effective loop bandwidth
  (put measured ~336MB/s + read ~63MB/s), the c=64 x 3.5MB = 224MB queue
  backlog cannot form — rate limiting or higher concurrency is needed.
  Eviction churn confirmed (F1 mechanism live): 5540 not_found + 5285 reseeds
  in 10 min (hot-set key leases expire between reads -> BatchEvict clears them
  -> re-read -> re-put). -703 (REPLICA_IS_NOT_READY) x3: another manifestation
  of a Get racing a replica being evicted/rewritten.
- **DoD run 2** (dod2_tc800m_ttl1000_c64_seed42): per the plan ladder, added
  `tc netem rate 800mbit` (=100MB/s, the "100 MB/s" row of the plan's lookup
  table; window 224MB/100MB/s ≈ 2.2s > TTL=1s). Results in the corresponding
  tierA_*.json.
- **DoD run 3** (dod3, er 0.1->0.5, tc 800mbit, TTL=1000): guard_fires=6709
  (both channels agree), not_found=1583 (hot-set eviction markedly increased),
  **rpe_events=0**. Structural analysis: er=0.5 clears all candidates in one
  round -> usage drops below the watermark -> trigger interval stretches to
  ~13s (45 rounds/600s); an in-flight object whose lease expires must wait for
  the next trigger round to be evictable, while the overstay window is only
  ~1-2s — **trigger rate** becomes the bottleneck; and under oldest-first
  ordering, freshly expired objects queue behind B's stock of never-read
  fodder.
- **DoD run 4** (dod4, B all with_soft_pin + er=0.5 + tc 800mbit): B's objects
  leave the pass-1/2 candidate set (master_service.cpp:6597 IsSoftPinned
  skip), steering eviction toward A's expired objects; B's objects are only
  touched in the pass-3 fallback (allow_evict_soft_pinned=1 kept at the
  deployment default). Soft-pin TTL 30min default. **Pure workload-side change
  (ReplicateConfig at Put); master protection parameters untouched.**
- Concluding mechanism (for the paper): RPE requires three factors to hold
  simultaneously — (i) guard trips (window formula: backlog/bandwidth > TTL);
  (ii) eviction trigger rate (usage persistently above watermark); (iii) a
  candidate queue shallow enough that a freshly expired object can be selected
  within its overstay (the front-advance speed of the oldest-first queue).
  run2/run3 lacked (iii)/(ii) respectively, hence thousands of guard trips
  with zero RPE — itself direct evidence that the lease guard is effective
  but not sufficient.
- **DoD run 5** (dod5, tc 400mbit=50MB/s, B soft-pin, er=0.5, v2 probe):
  guard_fires=6590 (both channels agree), expired_by p50=**3242ms** (binding
  broken mid-transfer for over 3 seconds), transfer p50=3753ms — the window is
  severely overrun; yet rpe_events=0, torn=0: all 6590 discarded buffers had
  correct head/tail markers (found (tenant=1,gen=1) in 6526 cases + gen=2 in
  64 cases). Meaning: **binding breakage is the norm (6590 times), but the
  slot was not overwritten inside the breakage window**. Cause analysis: B's
  puts were rate-limited to ~3.9/s -> reuse rate << expiry rate (11+/s) ->
  the oldest-first candidate queue backlogged (not_found=3017 corroborates)
  -> freshly expired in-flight objects never reached the queue front. Reuse
  is only visible as "evict-then-write": with triggered eviction (runs only
  when usage crosses the line) plus low-rate backpressure, slot release and
  reallocation do not synchronize.
- **DoD run 6** (dod6): pressure switched to max-rate backpressure (8
  concurrent putters, unpaced), so that every B put forces a candidate clear —
  testing the "evict-reuse-overwrite" synchronization chain.
- **DoD run 6 (breakthrough)** (dod6, B full-rate backpressure 8 putters +
  soft-pin + er=0.5 + tc 400mbit, TTL=1000, 600s): guard_fires=3589, eviction
  60 rounds, **rpe_events=2 + torn_events=1 (rpe_payload_bytes=7.34MB + torn
  3.67MB)**, success_mismatch=0. The three events (evidence chain: event file
  + per-request exp_gen join):
  1. hot/0230: exp_gen=1, read gen=2 (same key, new incarnation; overstay 2.40s)
  2. hot/0203: head=own gen=1, **tail=tenant 2 foreign object** (cross-tenant
     overwrite mixed in; overstay 2.30s)
  3. hot/0380: exp_gen=1, read gen=2 (overstay 0.54s)
  Mechanism closed loop: B's full-rate backpressure makes
  "evict -> reuse -> overwrite" complete synchronously inside the overstay;
  all events were caught by the lease guard (detect-and-discard works), but
  the wrong bytes had already been read back over the data path before the
  discard — the RPE thesis ("the guard discards, but the bytes were already
  on the wire") holds directly. Note: under max-rate, pressure saw 55333 put
  errors (backpressure behavior when the pool fills with soft-pinned objects;
  error codes to be classified); B succeeded 8454 puts / 31GB.
- Metric-convention addendum: same-key-different-gen (new incarnation) and
  foreign key_hash (foreign object) are reported separately: rpe_events counts
  2 same-key wrong-gen, torn counts 1 (foreign); the paper's McRaces may
  report the sum = 3 (conservative) or the split.
- **Run 6 review and convention correction (important)**: the 3 candidate
  events were re-checked against per-request timelines (req log
  t_lookup/exp_gen/rc + reseed completion timestamps):
  * hot/0230 and hot/0380 with found_gen=2 were in fact **stale-ledger
    artifacts** — the objects had been evicted earlier (consecutive -704s);
    the Get's GetReplicaList actually returned the reseeded gen=2 replica (a
    legitimate read), while the driver's claim-time ledger still read gen=1;
    **not counted as RPE**, reclassified as gen_skew_events=2.
  * hot/0203 is the **only unambiguous RPE**: head=own gen=1, tail=tenant 2
    foreign object (cross-tenant overwrite mixed in; no ledger explanation
    possible). Corrected run 6: guard_fires=3589, **rpe_events=1
    (rpe_payload_bytes=3.67MB, torn)**, gen_skew=2, misbw=13.17GB,
    success_mismatch=0.
  * **Convention correction (applies throughout)**: rpe_events /
    rpe_payload_bytes count only discard events with a **foreign key_hash**
    (head or tail) — same key with a different gen cannot distinguish "slot
    reuse" from "legitimately reading the reseeded newer gen" (RPC timing is
    unavailable), so those are always listed separately as gen_skew. The
    success_mismatch (red line) rule matches: only foreign or head/tail
    incoherent counts; a coherent newer gen of the same key is legitimate
    (reseed race).
  * Lesson: hot-set reseeds themselves produce "same key, new gen" slot reuse
    — a natural amplifier of RPE — but measurement must report it separately
    from foreign overwrites, otherwise the figure is inflated.
- **Tier-A validation cell** (evictaggr, TTL=5000, c=64, tc 300mbit, 30min):
  guard_fires=1564 (both channels agree), rpe=0, misbw=5.7GB,
  success_mismatch=0. The tc calibration for TTL=5000 (300mbit) holds ->
  the matrix TC_BY_TTL={1000:800, 5000:300, 11000:150}mbit is valid.
- **Phase 5** (expB, TTL=5000, tc 800mbit, 30min each): results in
  expB_pin_cliff.json. pinned/unpinned throughput ratio 1.068; eviction
  attempts 41273 vs 538 (77x, success rate 1.2%); non-reclaimable capacity
  39.3% vs 0%; hot-set losses 221 vs 1376 (-84%). Note: at this operating
  point (800mbit, TTL=5000) transfers take ~1s < TTL, so both arms have
  guard_fires=0 — pin's benefit/cost is unrelated to the guard, showing the
  two are orthogonal mechanisms (with full-coverage pinning there is no
  "expiry window" at all).
- **Tier-B validation** (tierB_d2000, delay=2000ms=2xTTL, evictaggr churn,
  30min, 7200 iterations): Query stage -704=7117 (under heavy churn hot keys
  are often already evicted); all completed transfers tripped the guard
  (guard_fires=75, gets_ok=0), **rpe_events=5 (rpe_payload_bytes=17.5MB, all
  head/tail-coherent foreign objects: 4 tenant-2 bulk objects + 1 same-tenant
  wrong key)**, success_mismatch=0. Interpretation: inside the constructed
  window (2s), "evict -> reuse -> full overwrite" completes comfortably and
  the reader pulls back a fully written new object — although the trip rate is
  low (75/7200; most queries fail earlier with -704), once a transfer starts
  and the binding breaks, 6.7% read back a completely wrong object. Tier-B is
  a structural upper-bound measurement, reported separately from Tier-A in the
  paper ("constructed": true).
- The rpe_probe zeroes the buffer on Query-failed iterations (prevents marker
  pollution from bytes of earlier iterations); the Tier-A probe is unaffected
  (at -707 the buffer is always a complete read of the current transfer).
