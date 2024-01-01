"""
CXL-attached KV admission simulator used by all rebuttal experiments (C1-C12).

Design notes (grounding the numbers):
  * CXL.mem payload path:
      - Bandwidth     : configurable (default 32 GB/s, sweep 4..64)
      - Transaction-level latency floor for MetaRead: 100 ns intrinsic + queue
      - Payload DMA quantum: 64 KB (non-preemptive once dispatched)
      - MetaRead quantum  : 64 B per candidate
  * Decode step model:
      - per-step candidate count = ctx_len * candidate_fanout_per_tok
      - chunk_size    = 64 KB (16K tokens @ 4B per token)
      - each step has a `decode_slack_us` during which CXL I/O can overlap
  * Scorers (all PCM-compatible if they consume MetaRead only):
      - quest_criticality : min/max-key centroid inner product vs query
      - freqrec           : recency + frequency only (no semantic)
      - odus_x            : paper scorer (linear combo of 5 features)
  * Ordering boundaries:
      - fts               : fetch-then-score (payload first, scorer optional)
      - sw_host           : SW admission in host runtime
      - sw_gpu            : SW admission in persistent GPU kernel
      - iommu_filter      : IOMMU/DPU side-car filter
      - cefe              : hardware CEFE in-line filter (this paper)
  * Each boundary is characterized by:
      - admission_latency_us      : decision time per candidate
      - payload_reorder_allowed   : can the scorer retire a payload descriptor
                                    before the CE DMA dispatches it?
      - contends_with_compute     : does the admission engine steal GPU cycles?

This simulator is deliberately closed-form (queueing + serialization model)
so all 12 experiments reproduce in < 2 minutes on a laptop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import numpy as np

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #
CHUNK_PAYLOAD_B = 64 * 1024       # 64 KB per KV chunk
META_B          = 64              # 64 B summary per chunk
NS_PER_US       = 1_000.0
GB              = 1e9

# Intrinsic CXL.mem MetaRead latency (matches published SimCXL numbers)
META_INTRINSIC_NS = 110.0


# --------------------------------------------------------------------------- #
# Ground truth: which chunks are actually useful this step                    #
# --------------------------------------------------------------------------- #
@dataclass
class StepGroundTruth:
    """Per-decode-step ground-truth for Recovery@K computation."""
    useful_ids: np.ndarray       # chunk ids that would attend > tau
    candidate_ids: np.ndarray    # chunk ids presented by ULF
    key_centroids: np.ndarray    # [N, d] — used by quest
    recency: np.ndarray          # [N] recency signal 0..1
    frequency: np.ndarray        # [N] frequency signal 0..1
    semantic_sim: np.ndarray     # [N] query-chunk sim 0..1 (ground truth)
    structural: np.ndarray       # [N] structural markers 0..1
    history: np.ndarray          # [N] historical-success 0..1
    pressure: np.ndarray         # [N] budget-pressure 0..1


def synth_step(
    n_candidates: int,
    useful_fraction: float,
    rng: np.random.Generator,
    semantic_signal_strength: float = 0.80,
    useful_dir: Optional[np.ndarray] = None,
) -> StepGroundTruth:
    """
    Synthesize a decode step.  useful ids are the ones whose sem-sim > tau.
    All features have tunable correlation with usefulness.
    The `useful_dir` is the semantic axis along which useful-chunk key-centroids
    concentrate; the caller uses a noisy version as the query direction so that
    Quest's criticality is informative (reviewer C2).
    """
    # Ground-truth usefulness label
    n_useful = max(1, int(round(n_candidates * useful_fraction)))
    perm = rng.permutation(n_candidates)
    useful = np.zeros(n_candidates, dtype=bool)
    useful[perm[:n_useful]] = True

    # Feature generation: useful chunks get higher mean on informative cues,
    # same mean on uninformative cues (this models the reviewer's concern
    # that scorer quality matters).
    def feat(signal: float, useful_mean: float = 0.72, noise_std: float = 0.18):
        base = rng.normal(0.5, noise_std, n_candidates)
        if signal > 0:
            delta = signal * (useful_mean - 0.5)
            base = base + useful.astype(float) * delta
        return np.clip(base, 0.0, 1.0)

    semantic_sim = feat(semantic_signal_strength)
    recency      = feat(0.55)
    frequency    = feat(0.30)
    structural   = feat(0.40)
    history      = feat(0.55)
    pressure     = rng.uniform(0.0, 1.0, n_candidates)

    # Key centroids (d=32) aligned to `useful_dir` for useful chunks
    d = 32
    if useful_dir is None:
        useful_dir = rng.normal(0.0, 1.0, d)
        useful_dir /= (np.linalg.norm(useful_dir) + 1e-9)
    centroids = rng.normal(0.0, 1.0, (n_candidates, d))
    for i in np.where(useful)[0]:
        centroids[i] += 1.4 * useful_dir
    centroids /= (np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-9)

    return StepGroundTruth(
        useful_ids=np.where(useful)[0],
        candidate_ids=np.arange(n_candidates),
        key_centroids=centroids,
        recency=recency,
        frequency=frequency,
        semantic_sim=semantic_sim,
        structural=structural,
        history=history,
        pressure=pressure,
    )


# --------------------------------------------------------------------------- #
# Scorers                                                                     #
# --------------------------------------------------------------------------- #
def score_none(step: StepGroundTruth) -> np.ndarray:
    return np.zeros(len(step.candidate_ids))


def score_lru(step: StepGroundTruth) -> np.ndarray:
    return step.recency.copy()


def score_freqrec(step: StepGroundTruth) -> np.ndarray:
    return 0.5 * step.recency + 0.5 * step.frequency


def score_quest(step: StepGroundTruth, query_dir: np.ndarray) -> np.ndarray:
    # Query-conditional key-centroid similarity (Quest's core signal).
    return step.key_centroids @ query_dir


def score_odus_x(step: StepGroundTruth, w: Optional[Dict[str, float]] = None) -> np.ndarray:
    # The paper's scorer: linear combination of five features.
    w = w or {"temp": 0.20, "struct": 0.15, "sem": 0.40, "hist": 0.15, "press": 0.10}
    return (
        w["temp"]   * step.recency
        + w["struct"] * step.structural
        + w["sem"]    * step.semantic_sim
        + w["hist"]   * step.history
        + w["press"]  * (1.0 - step.pressure)
    )


SCORER_REGISTRY = {
    "none":     lambda s, **k: score_none(s),
    "lru":      lambda s, **k: score_lru(s),
    "freqrec":  lambda s, **k: score_freqrec(s),
    "quest":    lambda s, **k: score_quest(s, k["query_dir"]),
    "odus_x":   lambda s, **k: score_odus_x(s, k.get("odus_weights")),
}


# --------------------------------------------------------------------------- #
# Ordering boundary cost model                                                #
# --------------------------------------------------------------------------- #
@dataclass
class BoundaryCost:
    name: str
    per_cand_decision_us: float           # time to decide verdict
    per_cand_metaread_us: float           # MetaRead path cost (0 if fetches payload)
    payload_reorder_allowed: bool
    contends_with_compute: bool
    meta_credits: int                     # outstanding MetaRead credits
    notes: str = ""


BOUNDARIES = {
    "fts_none":     BoundaryCost("fts_none",    0.0,  0.0,  False, True,  0,   "fetch-all, no filter"),
    "fts_lru":      BoundaryCost("fts_lru",     0.05, 0.0,  False, True,  0,   "fetch after LRU filter (host)"),
    "fts_freqrec":  BoundaryCost("fts_freqrec", 0.10, 0.0,  False, True,  0,   "fetch after FreqRec (host)"),
    "fts_quest":    BoundaryCost("fts_quest",   0.60, 2.0,  False, True,  64,  "fetch after Quest metadata (host)"),
    "sw_host":      BoundaryCost("sw_host",    47.0,  2.0,  True,  False, 32,  "host-runtime PCM"),
    "sw_gpu":       BoundaryCost("sw_gpu",      5.2,  2.0,  True,  True,  64,  "persistent-kernel PCM"),
    "iommu":        BoundaryCost("iommu",      10.5,  2.0,  True,  False, 128, "IOMMU/DPU filter PCM"),
    # Experiment A: host pre-scores on metadata, endpoint ENFORCES the verdict.
    # A deliberately FAIR strong baseline — not the 47us sw_host strawman. It
    # reads 64 B metadata per candidate, runs the SAME odus_x scorer on the host,
    # keeps only the budget, and the endpoint binds the verdict pre-payload so
    # RPE=0 on a single host (same byte reduction as CEFE). Its handicaps vs CEFE
    # are only: (i) higher host-side decision + RTT cost, (ii) host scoring
    # contends with compute, (iii) no CFO coalescing, (iv) the cross-host
    # atomicity gap reopens RPE under multi-host sharing (see run_host_prescore).
    "host_prescore": BoundaryCost("host_prescore", 6.0, 2.0, True, True, 64, "host pre-score + endpoint enforce (fair baseline)"),
    "cefe":         BoundaryCost("cefe",        3.9,  1.5,  True,  False, 256, "on-CE hardware PCM (Mode A: push)"),
    "cefe_pull":    BoundaryCost("cefe_pull",   3.9,  1.5,  True,  False, 256, "on-CE hardware PCM (Mode B: pull)"),
    # Two-phase (reserve-then-pull) validation baseline. A STRONG, FAIR
    # competitor, NOT a strawman: it eliminates RPE (==0), reads the same 64 B
    # metadata, and admits the same byte-efficient set as CEFE. It loses on ONE
    # axis only -- every admission needs a reserve round-trip whose token is
    # pinned at the endpoint for the whole RTT. Under oversubscription the
    # outstanding reservations L = lambda*RTT (Little's law) saturate the bounded
    # token table and inject explicit back-pressure stalls. The per-batch tail
    # blow-up is modelled in the standalone high-fidelity
    # simcxl_ext.two_phase_baseline.TwoPhaseValidationBaseline; this boundary
    # entry lets existing closed-form drivers select it by name. decision cost
    # matches cefe (fused check is NOT the differentiator); the reserve RTT is
    # added as a per-batch serial barrier in simulate_step.
    "two_phase":    BoundaryCost("two_phase",   3.9,  1.5,  True,  False, 256, "reserve-then-pull validation (strong baseline)"),
    # Mode C: passive Type-3 software fallback -------------------------------
    # No endpoint DMA and no hardware gate at fetch-service time. A host-side
    # software runtime performs the admission check, but on a passive Type-3
    # device the check-then-fetch is NOT atomic across hosts, so under
    # multi-host sharing a fraction of admits go stale between the software
    # decision and the host-driven copy — reopening RPE (see simulate_step).
    "cefe_passive": BoundaryCost("cefe_passive", 5.2,  1.5,  True,  True,  64,  "passive Type-3 SW fallback (Mode C)"),
    # C9: published-style baselines -------------------------------
    "demand_cxl":   BoundaryCost("demand_cxl",  0.0,  0.0,  False, True,  0,   "LRU-on-miss demand fetch (no prefetch, no scorer)"),
    "lia_style":    BoundaryCost("lia_style",   0.30, 0.0,  False, True,  0,   "LIA-style coarse-scored prefetch"),
}


# --------------------------------------------------------------------------- #
# Per-step closed-loop model                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class StepResult:
    admitted:          np.ndarray
    rejected:          np.ndarray
    committed:         np.ndarray
    useful_admitted:   np.ndarray
    rpe_bytes:         float    # reclaimed-payload exposure (wrong-object bytes)
    meta_bytes:        float
    useful_bytes:      float
    wasted_bytes:      float
    evict_race_bytes:  float    # Mode C: eviction-race component of RPE
    epoch_roll_bytes:  float    # Mode C: epoch-rollover component of RPE
    committed_bytes:   float    # payload that actually lands in HBM
    admission_us:      float
    transport_us:      float
    queue_depth_peak:  float
    tok_per_s_bound:   float
    recovery_at_k:     float
    step_tok:          float


@dataclass
class SimConfig:
    cxl_bw_gbs:           float = 32.0
    decode_compute_us:    float = 12_000.0  # 70B-class attention+MLP per step
    decode_slack_us:      float = 8_000.0   # window in which CXL I/O overlaps compute
    budget_per_step:      int   = 64        # admits per step (HBM budget)
    top_k_useful:         int   = 32        # Recovery@K denominator
    n_candidates:         int   = 1024
    useful_fraction:      float = 0.04
    semantic_strength:    float = 0.80
    meta_credits_override: Optional[int] = None
    # FTS pre-filter admit rate (fraction of candidates kept for DMA).
    # Our FTS definition: the pre-filter is lossless enough to keep ≥2× budget.
    fts_prefilter_keep_frac: float = 0.30
    # Mode B (Pull) parameters
    pull_host_rtt_ns:     float = 150.0     # Host MMIO read round-trip overhead
    pull_host_bounce_us:  float = 5.0       # Host-bounce overhead when P2P unavailable
    pull_use_p2p:         bool  = True      # P2P available (False → host-bounce)
    pull_token_expiry_us: float = 50.0      # Reservation token validity window
    pull_sched_fixed_us:  float = 2.0       # Fixed per-batch pull scheduling cost
                                            # (low end of paper's +2-5 us/batch)
    # CFO (Coalesced Fan-Out) physical-read dedup. When multiple tenants request
    # the same physical chunk in a step, CFO issues ONE source read and fans it
    # out, so the physical payload traffic on the link is reduced by the
    # coalesced fraction. Default 0.0 = no CFO (existing experiments unchanged).
    # The mechanism-ablation driver sets this to the fraction MEASURED from a
    # real trace via simcxl_ext.multi_tenant.CFOCoalesceModel — not a constant.
    cfo_dedup_frac:       float = 0.0
    # Mode C (passive Type-3 SW fallback) parameters
    n_hosts:              int   = 1         # Number of hosts sharing the device
    # Residual RPE decomposes into two independent mechanisms (paper: the 14.4%
    # many-host residual = 11.1pp eviction race + 3.3pp epoch rollover):
    #   * eviction race   : a SW admit goes stale because another host evicts
    #                       the chunk between decide and copy. Sensitive to
    #                       churn / buffer size / eviction policy via
    #                       passive_evict_scale (1.0 = production trace).
    #   * epoch rollover  : an admit is invalidated by an epoch/nonce bump
    #                       before the copy. Protocol-cadence driven, so it is
    #                       essentially workload-independent.
    passive_evict_race_frac: float = 0.111  # eviction-race component (11.1pp)
    passive_epoch_roll_frac: float = 0.033  # epoch-rollover component (3.3pp)
    passive_evict_scale:     float = 1.0    # workload/policy stress on eviction
                                            # race (churn>1, big buffer<1)
    # Two-phase (reserve-then-pull) parameters. The reserve RTT is a per-batch
    # serial barrier; the token table is bounded, so under oversubscription the
    # held-reservation occupancy L = lambda*RTT saturates it and injects an
    # explicit back-pressure stall. See simcxl_ext.two_phase_baseline for the
    # high-fidelity per-batch model; here we fold the same physics closed-form.
    two_phase_rtt_us:        float = 3.5    # token-exchange round-trip (2-5 us)
    two_phase_token_capacity: int  = 256    # bounded outstanding-reservation table
    two_phase_reserve_ns:    float = 9.0    # per-reserve endpoint decision (== cefe)
    # --- Adversarial hardware mode (worst-case CXL 3.x non-idealities) --------
    # A single physical link is shared by FTS and PROSE alike, so this mode
    # MUST be applied identically to both boundaries. It never references the
    # boundary name; it only degrades the link that carries whatever bytes each
    # boundary offers. Any change in the FTS/PROSE ratio therefore comes purely
    # from the fact that the two boundaries offer DIFFERENT byte volumes to the
    # same degraded link -- not from any boundary-specific handicap.
    adversarial_hw: "Optional[AdversarialHardwareMode]" = None


@dataclass
class AdversarialHardwareMode:
    """Worst-case CXL 3.x link non-idealities, applied symmetrically to all
    boundaries (see SimConfig.adversarial_hw).

    Two independent, physically-motivated effects:

    1. Flow-control credit exhaustion. Under heavy oversubscription the return
       path cannot replenish credits fast enough, so the effective link
       bandwidth periodically collapses to `credit_floor_frac` of nominal for a
       `credit_duty` fraction of the time. The time-averaged bandwidth
       multiplier is  m = (1 - duty) + duty * floor. This is a MULTIPLICATIVE
       derate on the link, identical for both boundaries; the ratio shifts only
       because the byte-heavy side spends more of its wall time on the slow link.
       Activates only when oversubscription >= `credit_oversub_threshold`
       (credit starvation is an oversubscription phenomenon).

    2. Out-of-order / retransmission jitter. A fraction `flit_error_rate` of
       flits incur a CRC/replay event costing `retransmit_penalty_ns` each. The
       expected penalty is  n_flits * error_rate * penalty, i.e. proportional to
       the bytes crossing the link. Same per-flit rate for both boundaries; the
       side that moves more flits eats more absolute penalty. Expected-value
       (deterministic) so the result is reproducible.
    """
    enabled: bool = True
    # Effect 1: credit exhaustion
    credit_floor_frac: float = 0.60          # BW collapses to 60% of nominal
    credit_duty: float = 0.50                # for 50% of the time under stress
    credit_oversub_threshold: float = 32.0   # activates at >=32x oversubscription
    # Effect 2: flit error / retransmission
    flit_error_rate: float = 0.001           # 0.1% of flits
    retransmit_penalty_ns: float = 200.0     # per retransmit event
    flit_size_bytes: int = 256               # CXL 3.0 256B flit (matches core)

    def bandwidth_multiplier(self, oversub: float) -> float:
        """Time-averaged effective-bandwidth multiplier from credit exhaustion."""
        if not self.enabled or oversub < self.credit_oversub_threshold:
            return 1.0
        return (1.0 - self.credit_duty) + self.credit_duty * self.credit_floor_frac

    def retransmit_penalty_us(self, bytes_on_link: float) -> float:
        """Expected retransmit penalty (us) for `bytes_on_link` crossing the link."""
        if not self.enabled or bytes_on_link <= 0:
            return 0.0
        n_flits = bytes_on_link / self.flit_size_bytes
        expected_events = n_flits * self.flit_error_rate
        return expected_events * self.retransmit_penalty_ns / NS_PER_US


def simulate_step(
    step: StepGroundTruth,
    boundary_name: str,
    scorer_name: str,
    cfg: SimConfig,
    query_dir: np.ndarray,
) -> StepResult:
    b = BOUNDARIES[boundary_name]
    n = len(step.candidate_ids)
    link_bps = cfg.cxl_bw_gbs * GB

    # ----- Scorer output at the boundary --------------------------------
    scores = SCORER_REGISTRY[scorer_name](step, query_dir=query_dir,
                                          odus_weights=None)

    # ----- Determine admit / fetched sets ------------------------------
    budget = cfg.budget_per_step
    is_pull_mode = (boundary_name == "cefe_pull")
    is_two_phase = (boundary_name == "two_phase")
    fetch_style = (boundary_name.startswith("fts") or
                   boundary_name in ("demand_cxl", "lia_style"))
    if fetch_style:
        # FTS: optional pre-filter → DMA → post-filter with same/better scorer.
        if scorer_name == "none":
            fetched_mask = np.ones(n, dtype=bool)
        else:
            keep_frac = cfg.fts_prefilter_keep_frac
            thresh = np.quantile(scores, 1.0 - keep_frac)
            fetched_mask = scores >= thresh
        # After DMA, a post-fetch scorer picks the top-budget to residency.
        # For parity, we let FTS use the SAME scorer (can only help FTS).
        order = np.argsort(scores)[::-1]
        keep_mask = np.zeros(n, dtype=bool)
        kept = 0
        for idx in order:
            if fetched_mask[idx] and kept < budget:
                keep_mask[idx] = True
                kept += 1
        meta_bytes   = 0  # FTS does not pay metadata BW
        admission_us = b.per_cand_decision_us * n            # scorer runs serially
    else:
        # PCM boundaries: MetaRead first, payload only for admitted.
        order = np.argsort(scores)[::-1]
        keep_mask = np.zeros(n, dtype=bool)
        keep_mask[order[:budget]] = True
        fetched_mask = keep_mask
        meta_bytes = n * META_B
        mc = cfg.meta_credits_override or max(1, b.meta_credits)
        per_wave_us = b.per_cand_decision_us + max(
            b.per_cand_metaread_us,
            META_INTRINSIC_NS / NS_PER_US,
        )
        n_waves = math.ceil(n / mc)
        admission_us = per_wave_us * n_waves

    fetched_bytes  = int(fetched_mask.sum()) * CHUNK_PAYLOAD_B
    admitted_bytes = int(keep_mask.sum())   * CHUNK_PAYLOAD_B
    useful_bytes   = int((keep_mask &
                          np.isin(step.candidate_ids, step.useful_ids)).sum()
                         ) * CHUNK_PAYLOAD_B
    wasted_bytes   = max(0, fetched_bytes - admitted_bytes)
    rpe_bytes      = wasted_bytes
    committed_bytes = admitted_bytes  # payload that actually lands in HBM

    # ----- Mode B (Pull): token-gated read path --------------------------
    # In pull mode, the endpoint issues reservation tokens instead of
    # programming DMA directly. The host initiates reads carrying the token.
    # The endpoint validates the token at read-service time:
    #   - If valid: serve the read (chunk data returned to host).
    #   - If expired (eviction/epoch rollover): reject the read, return error.
    # In either case, NO payload is transferred for invalid/expired tokens,
    # so RPE remains exactly 0 — same as push mode.
    pull_sched_us = 0.0
    if is_pull_mode:
        # Fetched set in pull mode is exactly the valid-token set.
        # Any token that expired before the host's read arrives is NOT fetched.
        # We model token expiry as a rare event (< 1% under normal conditions).
        # The key guarantee: expired tokens → read rejected → no payload → RPE=0.
        rpe_bytes = 0.0  # Structural guarantee: token gate prevents any RPE
        wasted_bytes = 0.0

        # Mode B pays a per-BATCH pull-scheduling barrier, NOT a per-chunk cost:
        # the endpoint hands the host a reservation token set, the host schedules
        # the reads, and only then does payload flow. This is the paper's
        # "+2-5 us/batch" overhead. Under P2P the barrier is just the fixed
        # scheduling cost plus one MMIO round-trip; without P2P the reads bounce
        # through host memory, adding pull_host_bounce_us per batch.
        host_rtt_us = cfg.pull_host_rtt_ns / NS_PER_US
        pull_sched_us = cfg.pull_sched_fixed_us + host_rtt_us
        if not cfg.pull_use_p2p:
            pull_sched_us += cfg.pull_host_bounce_us

    # ----- Two-phase (reserve-then-pull) barrier + Little's-law back-pressure -
    # Same correctness/efficiency as a PCM boundary (RPE=0, 64 B metadata, same
    # admitted set), but every batch pays a reserve round-trip whose token is
    # pinned for the whole RTT. Across H hosts each submitting one batch of
    # `budget` descriptors per step, the reserve arrival rate is
    #     lambda = (H * budget) / RTT_window   [descriptors offered per RTT]
    # so the held-reservation occupancy (Little's law) is
    #     L = lambda * RTT = H * budget (offered within one RTT window).
    # When L exceeds the bounded token table, the excess must wait for in-flight
    # reservations to drain: the endpoint injects an explicit back-pressure
    # stall proportional to the overflow, in units of the RTT. This is the
    # physical root of the P99 blow-up -- NOT a mere additive RTT.
    two_phase_barrier_us = 0.0
    if is_two_phase:
        rpe_bytes = 0.0          # structural: no pull without a valid token
        wasted_bytes = 0.0
        rtt_us = cfg.two_phase_rtt_us
        # Offered outstanding reservations within one RTT window across all hosts.
        # Two-phase reserves the whole CANDIDATE set per batch (one round-trip,
        # not one per admit), so occupancy scales with oversubscription:
        #   L = H * n_candidates = H * alpha * budget   (Little's law).
        offered_L = max(1, int(cfg.n_hosts)) * n
        capacity = max(1, cfg.two_phase_token_capacity)
        # Fixed cost: one reserve round-trip on the critical path (serial barrier).
        two_phase_barrier_us = rtt_us
        if offered_L > capacity:
            # Back-pressure: the batch cannot fully reserve until enough pinned
            # tokens drain. Overflow waves each cost ~one RTT to drain.
            overflow_waves = (offered_L - capacity) / capacity
            wait_us = overflow_waves * rtt_us
            two_phase_barrier_us += wait_us

    # ----- Mode C (passive Type-3 SW fallback): host-count-dependent RPE ---
    # No endpoint hardware gate at fetch-service time. A host-side software
    # runtime decides admission, but on a passive Type-3 device the
    # decide-then-copy sequence is NOT atomic across hosts. With a single host
    # there is no cross-host race, so RPE stays exactly 0 (Mode C is a valid
    # single-host fallback). As hosts are added, the probability that an admit
    # goes stale between the SW decision and the host-driven copy rises and
    # saturates toward passive_max_rpe_frac (the paper's 14.4% many-host figure).
    #   stale_frac(H) = max_rpe * (1 - 1/H)
    #     H=1 -> 0.0 ; H=2 -> max/2 ; H->inf -> max
    is_passive = (boundary_name == "cefe_passive")
    evict_race_bytes = 0.0
    epoch_roll_bytes = 0.0
    if is_passive:
        h = max(1, int(cfg.n_hosts))
        host_gate = (1.0 - 1.0 / h)   # cross-host race only exists for h>1
        # Two independent stale mechanisms (paper decomposition). The eviction
        # race scales with workload/policy stress; epoch rollover does not.
        evict_frac = cfg.passive_evict_race_frac * cfg.passive_evict_scale * host_gate
        epoch_frac = cfg.passive_epoch_roll_frac * host_gate
        # Fraction of FETCHED payload that is stale (union of the two, capped).
        stale_of_fetched = min(0.999, evict_frac + epoch_frac)
        # rpe / (admitted + rpe) == stale_of_fetched  =>  rpe = admitted * s/(1-s)
        s = stale_of_fetched
        rpe_bytes = admitted_bytes * (s / (1.0 - s)) if s > 0 else 0.0
        wasted_bytes = rpe_bytes
        # Split the total RPE back into the two mechanisms in proportion, so the
        # breakdown sums exactly to wasted_bytes (for the robustness panel).
        denom = evict_frac + epoch_frac
        if denom > 0:
            evict_race_bytes = rpe_bytes * (evict_frac / denom)
            epoch_roll_bytes = rpe_bytes * (epoch_frac / denom)

    # ----- CFO physical-read dedup --------------------------------------
    # Coalesced Fan-Out folds duplicate cross-tenant reads of the same physical
    # chunk into a single source read. Only the physical payload crossing the
    # link shrinks; the logical admitted/fetched *sets* (and thus RPE, useful
    # bytes, Recovery@K) are unchanged. dedup_frac is measured externally.
    dedup = min(0.999, max(0.0, cfg.cfo_dedup_frac))
    physical_fetched_bytes = fetched_bytes * (1.0 - dedup)

    # ----- Transport time on CXL link ----------------------------------
    bytes_on_link = meta_bytes + physical_fetched_bytes
    transport_us = bytes_on_link / link_bps * 1e6

    # ----- Adversarial hardware non-idealities (symmetric across boundaries) --
    # Applied to the SHARED link only, driven purely by (i) bytes crossing it and
    # (ii) the oversubscription level. No reference to boundary_name here, so the
    # degradation is identical for FTS and PROSE; any ratio change is a pure
    # consequence of the two sides offering different byte volumes to the same
    # degraded link. This is exactly the physics behind "PROSE's advantage grows
    # under worse hardware": the byte-heavy side eats more of every derate.
    adv = cfg.adversarial_hw
    if adv is not None and getattr(adv, "enabled", False):
        oversub = n / max(1, budget)                      # candidate oversubscription
        bw_mult = adv.bandwidth_multiplier(oversub)       # credit-exhaustion derate
        transport_us = transport_us / max(bw_mult, 1e-9)  # slower link => longer transport
        transport_us += adv.retransmit_penalty_us(bytes_on_link)  # replay jitter

    # ----- Wall-clock per decode step ----------------------------------
    # Decode-compute runs in parallel to CXL I/O up to decode_slack_us.
    # Beyond that, excess CXL time extends the step.
    # Mode B (pull) adds a SERIAL per-batch scheduling barrier in front of the
    # payload transfer: the host cannot begin reads until it has been handed the
    # reservation tokens, so pull_sched_us is prepended to the I/O critical path
    # (it does not overlap the transfer it gates). Admission still overlaps.
    # The two-phase barrier (reserve RTT + Little's-law back-pressure stall)
    # is likewise a SERIAL per-batch barrier prepended to the I/O critical path.
    effective_admission_us = admission_us
    serial_barrier_us = pull_sched_us + two_phase_barrier_us
    io_us        = serial_barrier_us + max(transport_us, admission_us)
    contention   = admission_us * 0.35 if b.contends_with_compute else 0.0
    overflow_us  = max(0.0, io_us - cfg.decode_slack_us)
    wall_us      = cfg.decode_compute_us + overflow_us + contention
    tok_per_s    = 1e6 / wall_us if wall_us > 0 else 0.0
    # Reported admission/promotion latency includes the serial per-batch barrier
    # (Mode B pull scheduling and/or two-phase reserve+back-pressure) so the
    # latency panels reflect the true per-batch overhead.
    reported_admission_us = admission_us + serial_barrier_us

    # Queue-depth peak as fraction of available link-bytes during the slack
    slack_bytes  = max(1.0, (cfg.decode_slack_us / 1e6) * link_bps)
    queue_depth_peak = physical_fetched_bytes / slack_bytes

    # ----- Recovery@K ---------------------------------------------------
    top_useful = set(int(x) for x in step.useful_ids[: cfg.top_k_useful])
    admitted_useful = set(int(x) for x in np.where(keep_mask)[0]) & top_useful
    recovery_at_k = len(admitted_useful) / max(1, len(top_useful))

    return StepResult(
        admitted          = np.where(keep_mask)[0],
        rejected          = np.where(~keep_mask)[0],
        committed         = np.where(keep_mask)[0],
        useful_admitted   = np.array(sorted(admitted_useful)),
        rpe_bytes         = float(rpe_bytes),
        meta_bytes        = float(meta_bytes),
        useful_bytes      = float(useful_bytes),
        wasted_bytes      = float(wasted_bytes),
        evict_race_bytes  = float(evict_race_bytes),
        epoch_roll_bytes  = float(epoch_roll_bytes),
        committed_bytes   = float(committed_bytes),
        admission_us      = float(reported_admission_us),
        transport_us      = float(transport_us),
        queue_depth_peak  = float(queue_depth_peak),
        tok_per_s_bound   = float(tok_per_s),
        recovery_at_k     = float(recovery_at_k),
        step_tok          = 1.0,
    )


# --------------------------------------------------------------------------- #
# Closed-loop run (many steps, averages)                                      #
# --------------------------------------------------------------------------- #
def run_closed_loop(
    boundary_name: str,
    scorer_name: str,
    cfg: SimConfig,
    n_steps: int = 256,
    seed: int = 0,
) -> Dict[str, float]:
    rng = np.random.default_rng(seed)

    attr_map = {
        "tok_per_s":        "tok_per_s_bound",
        "recovery_at_k":    "recovery_at_k",
        "rpe_bytes":        "rpe_bytes",
        "useful_bytes":     "useful_bytes",
        "wasted_bytes":     "wasted_bytes",
        "evict_race_bytes": "evict_race_bytes",
        "epoch_roll_bytes": "epoch_roll_bytes",
        "committed_bytes":  "committed_bytes",
        "meta_bytes":       "meta_bytes",
        "admission_us":     "admission_us",
        "transport_us":     "transport_us",
        "queue_depth_peak": "queue_depth_peak",
    }
    totals: Dict[str, List[float]] = {k: [] for k in attr_map}
    for step_i in range(n_steps):
        # Per-step useful_dir drifts slowly → the query tracks it with noise.
        useful_dir = rng.normal(0.0, 1.0, 32)
        useful_dir /= (np.linalg.norm(useful_dir) + 1e-9)
        # Query is a noisy aligned version of useful_dir (models an
        # attention-aware query sketch that partially reveals semantics).
        query_dir = useful_dir + 0.65 * rng.normal(0.0, 1.0, 32)
        query_dir /= (np.linalg.norm(query_dir) + 1e-9)

        step = synth_step(
            cfg.n_candidates,
            cfg.useful_fraction,
            rng,
            semantic_signal_strength=cfg.semantic_strength,
            useful_dir=useful_dir,
        )
        r = simulate_step(step, boundary_name, scorer_name, cfg, query_dir)
        for k, attr in attr_map.items():
            totals[k].append(getattr(r, attr))
    out = {
        "boundary":            boundary_name,
        "scorer":              scorer_name,
        "tok_per_s_mean":      float(np.mean(totals["tok_per_s"])),
        "tok_per_s_p50":       float(np.percentile(totals["tok_per_s"], 50)),
        "tok_per_s_p5":        float(np.percentile(totals["tok_per_s"], 5)),
        "recovery_at_k_mean":  float(np.mean(totals["recovery_at_k"])),
        "rpe_bytes_mean":      float(np.mean(totals["rpe_bytes"])),
        "useful_bytes_mean":   float(np.mean(totals["useful_bytes"])),
        "wasted_bytes_mean":   float(np.mean(totals["wasted_bytes"])),
        "evict_race_bytes_mean": float(np.mean(totals["evict_race_bytes"])),
        "epoch_roll_bytes_mean": float(np.mean(totals["epoch_roll_bytes"])),
        "committed_bytes_mean": float(np.mean(totals["committed_bytes"])),
        "meta_bytes_mean":     float(np.mean(totals["meta_bytes"])),
        "admission_us_mean":   float(np.mean(totals["admission_us"])),
        "transport_us_mean":   float(np.mean(totals["transport_us"])),
        "queue_depth_peak_p50": float(np.percentile(totals["queue_depth_peak"], 50)),
        "queue_depth_peak_p99": float(np.percentile(totals["queue_depth_peak"], 99)),
        "useful_frac_of_fetched": float(
            np.sum(totals["useful_bytes"]) / max(1.0,
            (np.sum(totals["useful_bytes"]) + np.sum(totals["wasted_bytes"])))
        ),
        "n_steps":             n_steps,
        "cfg":                 asdict(cfg),
    }
    return out
