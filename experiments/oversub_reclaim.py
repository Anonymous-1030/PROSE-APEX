"""Optimistic-Reclaim engine: transfer-scoped vs early protection under extreme
oversubscription (paper §IV-C/§IV-D, Experiment A).

WHY A DEDICATED ENGINE.  The seven-mechanism harness
(`experiments/baselines/baseline_common.py`) isolates *protection placement vs
duration* on a single serial link, and is tuned to reproduce Figure 3.  It does
not model the one effect that dominates at extreme oversubscription: a
*bounded pin/slot resource* that early-protection mechanisms exhaust because they
hold protection across the whole queue wait.  This module adds exactly that, as a
clean two-resource discrete-event model, while reusing the calibrated byte/timing
constants from `BaselineConfig` and the same paired-trace principle (identical
arrival, candidate, and eviction-attempt streams across mechanisms).

THE PHYSICS (the crux of the innovation upgrade).  An admitted transfer holds one
pin "license" for a *hold window* H:

    early protection (REFCNT / 2PHASE):  H = queue_wait + transfer_time
                                         (pin acquired at enqueue, before the
                                          endpoint even dequeues the descriptor)
    transfer-scoped   (PROSE):           H = transfer_time
                                         (pin acquired at admission, released at
                                          transfer completion)
    GENONLY:                              H = transfer_time, but NO transfer-span
                                         protection -> a mid-transfer reuse yields
                                         stale payload (RPE > 0); included as an
                                         incomplete baseline.

By Little's law the mean number of concurrently held licenses is L = lambda * H.
With only C licenses (the bounded on-chip pin/reservation table, e.g. 256
entries), early protection drives L toward C once
lambda * (queue_wait + transfer) >= C — and because queue_wait grows with
oversubscription, that threshold is crossed at moderate oversubscription.  Beyond
it, a new admission cannot acquire a license until an in-flight one releases, so
admission latency (and its P99) explodes and reclamation is blocked.  PROSE's
window is just the transfer, so L stays near the in-flight admit budget (<< C)
and admission never waits on the license — reclamation stays legal until a
transfer is actually admitted.

CROSS-VALIDATION.  The bounded resource is parameterized (`bound_mode`):
  * "capacity"    — C = endpoint slot capacity (physical slots a pinned object
                    occupies and thereby makes non-reclaimable).
  * "token_table" — C = bounded reservation/pin *token table* size (the §III-B
                    400-entry table / a reservation table).  Same Little's-law
                    mechanics, different resource, to show the conclusion is
                    robust to the modeling choice.

Everything else (arrivals, service time, eviction attempts) is shared verbatim
across mechanisms, so the comparison is paired.

DESIGN-SPACE EXTENSIONS (additive; paper rebuttal "REFCNT strawman").  Four
further `ProtMech` entries live in `MECHS` but NOT in `MECH_ORDER` (the
original drivers are untouched): REFCNT_S (host pre-selects and pins only the
admit-budget subset; identity-checked but NOT generation-checked pins -> stale
admits/retries from the snapshot->acquire window), RECLAIM_DEFER (reclaim-side
deferral: queued descriptors defer reclaim of their slots — same pre-enqueue
occupancy and cliff as REFCNT — with no admission re-check and no transfer-span
pin -> nonzero stale payload), GENONLY_EF (GENONLY + Tigon-style epoch fence:
the slot overwrite is deferred by one grace period —
`cfg.base.eviction_interval_ns`, one allocator epoch — so a raced transfer
stays valid for grace_ns beyond the reclaim request; the stale tail shrinks by
grace/service, it does not vanish), and PROSE_HOSTSEL (PROSE admitting exactly
the host's pre-selected subset; commit-time generation check -> zero stale,
showing selection authority does not affect safety). See `run_design_space.py`
and `run_design_space_epochfence.py`.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.baselines.baseline_common import BaselineConfig  # noqa: E402


# ── Mechanism protection model (only the hold window + RPE differ) ───────────
@dataclass(frozen=True)
class ProtMech:
    name: str
    label: str
    # when is the license (pin/reservation) acquired?
    #   "enqueue"   -> held from arrival across the queue wait (REFCNT / 2PHASE)
    #   "admission" -> held from service start (PROSE)
    #   "none"      -> no transfer-span pin at all (GENONLY: RPE possible)
    acquire: str
    protects_transfer: bool          # blocks mid-transfer reuse (no stale bytes)
    extra_rtt: int = 0               # serialized coordination round trips
    serialized_acquire_ns: float = 0.0   # one-time pipeline-fill cost
    # ── design-space study fields (additive; defaults reproduce the original
    #    four mechanisms exactly) ──
    # pin_scope: which candidates an early-protection ("enqueue") mechanism pins.
    #   "backlog" -> every built candidate reserves (REFCNT/2PHASE; RECLAIM_DEFER
    #                defers reclaim of every queued descriptor's slot — same
    #                occupancy, same cliff).
    #   "budget"  -> the host pre-selects ONLY the admit-budget-sized subset by
    #                its local scores and pins exactly those (REFCNT_S): pool
    #                occupancy stays ~= budget (<< C), so the Little's-law cliff
    #                disappears — at the cost below.
    pin_scope: str = "backlog"
    # snapshot_check: does pin acquisition validate the object's GENERATION
    # against the host's snapshot?  REFCNT_S validates identity only: a
    # reclaim+reincarnation (same id, new generation) in the snapshot->acquire
    # window attaches the pin to the wrong incarnation -> stale admit; a
    # simply-gone object fails the acquire and the host must re-select (retry).
    snapshot_check: bool = True
    # epoch_fence (GENONLY_EF only): Tigon-style EBR on the reclaim path. The
    # unlink is visible to admission checks immediately (== GENONLY), but the
    # slot overwrite is deferred by one grace period
    # (cfg.base.eviction_interval_ns, the allocator epoch), so a raced transfer
    # stays valid for grace_ns beyond the reclaim request.
    epoch_fence: bool = False


MECHS: Dict[str, ProtMech] = {
    "PROSE":  ProtMech("PROSE",  "PROSE (transfer-scoped pin)", "admission",
                       protects_transfer=True),
    "REFCNT": ProtMech("REFCNT", "RefCnt (protect before enqueue)", "enqueue",
                       protects_transfer=True, extra_rtt=1,
                       serialized_acquire_ns=250.0),
    "2PHASE": ProtMech("2PHASE", "2Phase (reserve + request)", "enqueue",
                       protects_transfer=True, extra_rtt=1,
                       serialized_acquire_ns=3500.0),
    "GENONLY": ProtMech("GENONLY", "GenOnly (one-shot check, no pin)", "none",
                        protects_transfer=False),
    # ── design-space study mechanisms (additive; NOT in MECH_ORDER, so the
    #    original Table-IV drivers `run_optimistic_reclaim.py` and
    #    `run_oversub_low.py` are untouched) ──
    "REFCNT_S": ProtMech("REFCNT_S",
                         "RefCnt-Subset (host-picked budget set, no gen check)",
                         "enqueue", protects_transfer=True, extra_rtt=1,
                         serialized_acquire_ns=250.0,
                         pin_scope="budget", snapshot_check=False),
    "RECLAIM_DEFER": ProtMech("RECLAIM_DEFER",
                              "Reclaim-side deferral (no xfer pin, no admit re-check)",
                              "enqueue", protects_transfer=False),
    "GENONLY_EF": ProtMech("GENONLY_EF",
                           "GenOnly + epoch fence (overwrite deferred 1 grace period)",
                           "none", protects_transfer=False, epoch_fence=True),
    "PROSE_HOSTSEL": ProtMech("PROSE_HOSTSEL",
                              "PROSE, host-selected subset (commit-time gen check)",
                              "admission", protects_transfer=True),
}
MECH_ORDER = ["PROSE", "REFCNT", "2PHASE", "GENONLY"]

# Wall time of one decode step (ns). Promotions for a step land within it; pins
# drain between steps. 1 ms is the paper's decode-step scale (§III-C: the 9 ns
# admit sits 2-3 orders below the 100 us decode step; we use 1 ms so multi-step
# pin holds under oversubscription are expressed in a realistic wall-clock).
DECODE_STEP_NS = 1_000_000.0


# ── Configuration ────────────────────────────────────────────────────────────
@dataclass
class OversubConfig:
    """One operating point for the oversubscription sweep."""
    oversubscription: int = 32        # candidates offered / admit budget
    n_tenants: int = 16
    admit_budget: int = 32            # admits/step per the shared budget (K)
    n_steps: int = 256
    # bounded pin/slot resource
    bound_mode: str = "capacity"      # "capacity" | "token_table"
    capacity: int = 256               # endpoint slots (bound_mode="capacity")
    token_table: int = 256            # reservation/pin token entries ("token_table")
    seed: int = 0
    base: BaselineConfig = field(default_factory=BaselineConfig)

    def bound(self) -> int:
        return self.capacity if self.bound_mode == "capacity" else self.token_table

    def service_ns(self) -> float:
        """Per-transfer service time (payload serialize + proto + DRAM), reusing
        the same calibrated constants as the seven-mechanism harness."""
        b = self.base
        payload = b.object_bytes / b.link_bw_gbps            # ns (GB/s == B/ns)
        proto = 2 * 15.0 + 50.0                              # proto proc + bridge
        dram = 120.0                                         # one DDR5 row-miss
        return payload + proto + dram


# ── Shared offered-load trace (paired across mechanisms) ─────────────────────
@dataclass
class OversubTrace:
    cfg: OversubConfig
    arrivals: np.ndarray             # sorted arrival times (ns)
    obj_ids: np.ndarray              # target object per request (Zipf-hot)
    race_xfer: np.ndarray            # bool: a reuse attempt fires mid-transfer
    cold_miss: np.ndarray            # bool: admitting requires reclaiming a slot
    n_requests: int
    # Design-space study streams (used ONLY by snapshot_check=False mechanisms,
    # i.e. REFCNT_S). Drawn LAST in `generate_oversub_trace`, after every
    # original draw, so obj_ids/race_xfer/cold_miss — and therefore every
    # original mechanism's replay — are bit-identical to before.
    race_snap: np.ndarray = None     # bool: object reclaimed in snapshot->acquire window
    reincarn: np.ndarray = None      # bool: ...and already reincarnated (same id, new gen)


def generate_oversub_trace(cfg: OversubConfig) -> OversubTrace:
    """ONE shared offered-load trajectory, structured by decode step.

    Each decode step the hosts offer `oversubscription * admit_budget` candidate
    promotions; the endpoint admits `admit_budget` of them (the shared budget)
    and the remaining `(oversub-1)*budget` stay queued and are re-offered in
    later steps. A candidate is BUILT (its descriptor + protection intent is
    created) at the step it first appears, and ADMITTED at the step it wins the
    budget — the gap between the two is the queue wait that early protection must
    hold a license across. All RNG lives here so every mechanism replays the
    identical build/admit/eviction-attempt stream (paired comparison)."""
    rng = np.random.default_rng(cfg.seed)
    per_step = cfg.oversubscription * cfg.admit_budget
    n_requests = per_step * cfg.n_steps

    # Each request's BUILD step is its first appearance. Zipf-hot object ids give
    # a realistic contended working set.
    build_step = np.repeat(np.arange(cfg.n_steps), per_step)
    working_set = max(cfg.oversubscription * cfg.capacity, per_step)
    obj_ids = (rng.zipf(1.2, size=n_requests) % working_set).astype(np.int64)

    # Mid-transfer reuse attempt per request (same attempts for every mechanism).
    race_xfer = rng.random(n_requests) < 0.35

    # Cold-miss flag: this promotion targets an object NOT currently resident, so
    # admitting it requires reclaiming a slot (evicting some resident object).
    # Same flags for every mechanism (paired). A cold admission can proceed only
    # if the endpoint can reclaim a slot — which early protection's pins block.
    cold_miss = rng.random(n_requests) < 0.5

    # Design-space streams (REFCNT_S). Drawn LAST so every original array above
    # is bit-identical to before. race_snap: the referenced object is reclaimed
    # in the host-snapshot -> pin-acquire window (same 0.35 attempt rate as the
    # mid-transfer stream — the paired attempt stream at a different window).
    # reincarn: given that reclaim, the slot has already been reincarnated with
    # the same id under a new generation (-> stale admit) vs simply gone
    # (-> acquire failure, host re-select = retry).
    race_snap = rng.random(n_requests) < 0.35
    reincarn = rng.random(n_requests) < 0.5

    return OversubTrace(cfg=cfg, arrivals=build_step.astype(np.float64),
                        obj_ids=obj_ids, race_xfer=race_xfer,
                        cold_miss=cold_miss, n_requests=n_requests,
                        race_snap=race_snap, reincarn=reincarn)


def replay_oversub(trace: OversubTrace, mech: ProtMech) -> Dict:
    """Replay the shared decode-step trajectory under one protection mechanism.

    Model (one decode step = DECODE_STEP_NS wall time, admits `admit_budget`):

      * A bounded pool of C licenses (pins/reservations/slots) is the contended
        resource. A license is HELD across the mechanism's protection window:
          - early protection (REFCNT/2PHASE, acquire="enqueue"): from the BUILD
            step to transfer completion. Under oversubscription a candidate waits
            many steps to be admitted, so its license is held across ALL those
            steps — L = (offered rate) * (build->complete span) grows with
            oversubscription and saturates C.
          - transfer-scoped (PROSE, acquire="admission"): only during the admit
            step's transfer. L stays ~= admit_budget (<< C), independent of
            oversubscription.
          - GENONLY (acquire="none"): no transfer-span pin; a mid-transfer reuse
            yields stale payload (RPE>0).

      * Each step we admit up to `admit_budget` waiting candidates, BUT an
        admission can only proceed if a license is available at its acquire
        point. Early protection must ALSO have held a license since its build
        step; if the pool was full when it was built, it could not even reserve,
        so its admission slips — this is the back-pressure that inflates P99 and
        blocks reclamation. PROSE acquires only at admit, so it is throttled only
        by the (tiny) in-step budget.

      * Admission latency for a request = (admit_step - build_step) * step_ns
        plus the intra-step admit cost. P99 is over all requests.
    """
    """Replay the shared offered-load trajectory under one protection mechanism.

    Two resources serialize the endpoint:
      * the LINK: one transfer serializes at a time (service time S).
      * C LICENSES: a bounded pin/reservation resource. A request cannot be
        ADMITTED (its transfer cannot start) until it holds a license. The
        license is acquired at `mech.acquire` and released at transfer completion.

    The whole story is in the license HOLD WINDOW:
      * PROSE  (acquire="admission"): license held only for the transfer, so at
        most ~admit_budget licenses are ever in flight -> admission never waits
        on a license, reclamation stays legal.
      * REFCNT/2PHASE (acquire="enqueue"): license conceptually held from arrival,
        so its window includes the queue wait. Under oversubscription the queue
        wait grows, L = lambda*(wait+S) -> C, and admission blocks on license
        availability -> P99 explodes and pinned objects cannot be reclaimed.
    """
    cfg = trace.cfg
    C = cfg.bound()
    n = trace.n_requests
    obj_bytes = cfg.base.object_bytes
    budget = cfg.admit_budget
    step_ns = DECODE_STEP_NS                    # per-step wall time
    build_step = trace.arrivals.astype(np.int64)
    races = trace.race_xfer
    cold_miss = trace.cold_miss

    # ── Per-step admission with a bounded license pool of size C ──
    #
    # The difference between mechanisms is WHEN a license is claimed relative to
    # admission, which sets how many are held concurrently (Little's law):
    #
    #   PROSE (acquire="admission"): a license is claimed ONLY for the admit
    #     step's transfer. Held licenses = this step's admits (<= budget), always.
    #     Admission is throttled only by the budget, never by the pool, so P99 is
    #     flat and reclaimable capacity stays ~ C - budget regardless of oversub.
    #
    #   REFCNT / 2PHASE (acquire="enqueue"): a license (refcount / reservation) is
    #     claimed when the candidate is BUILT and held until it is admitted. Under
    #     oversubscription `per_step` candidates are built each step but only
    #     `budget` drain, so the number of BUILT-but-not-yet-admitted descriptors
    #     — each holding a license — grows until it hits C. Once the pool is full,
    #     newly built candidates cannot reserve, so they cannot be admitted until a
    #     reservation releases: admission latency (P99) climbs and almost no
    #     capacity is reclaimable (held ~ C).
    #
    #   GENONLY (acquire="none"): admitted at budget like PROSE, holds no
    #     transfer-span license (so RPE>0 on raced transfers), pool never a factor.
    early = (mech.acquire == "enqueue")
    # REFCNT_S: early protection, but the host pins ONLY its pre-selected
    # admit-budget-sized subset instead of the whole offered backlog.
    subset = early and (mech.pin_scope == "budget")
    admit_step = np.full(n, -1, dtype=np.int64)

    PROSE_ADMIT_NS = 9.0                 # fused epoch-check + pin (RTL-measured)

    # COUNT-BASED per-step model (O(n_steps)). Requests within a step are
    # statistically identical (iid cold_miss / race), so we track COUNTS, not
    # individual indices, and assign admission steps to request ids in build
    # order afterward. This is exact for the metrics we report and ~1000x faster
    # than per-request list surgery.
    #
    # Candidates not admitted within one step are superseded by the next decode
    # step's fresher candidate set, so the backlog is bounded to the per-step
    # offered set (per_step). This makes pool occupancy TRACK oversubscription
    # rather than accumulate the whole trace.
    per_step = cfg.oversubscription * budget
    cold_frac = float(cold_miss.mean())      # ~0.5 by construction

    # per-step outputs
    step_admitted = np.zeros(cfg.n_steps, dtype=np.int64)
    step_held = np.zeros(cfg.n_steps, dtype=np.float64)
    step_admit_ns = np.full(cfg.n_steps, PROSE_ADMIT_NS, dtype=np.float64)

    for step in range(cfg.n_steps):
        offered = per_step                    # fresh candidates this step (backlog=1 step)
        if subset:
            # REFCNT_S: the host pre-selects the admit-budget-sized subset of the
            # candidate set by its local scores and pins exactly those (still
            # protect-before-enqueue, same serialized acquire as REFCNT). Pool
            # occupancy is therefore ~= budget (<< C) at ANY oversubscription:
            # the Little's-law cliff disappears. Pins release at transfer
            # completion; admission is throttled only by the in-step budget
            # (reclaimable slots = C - held >= cold wants for any pool >= ~2*budget).
            held = min(budget, offered, C)
            admitted = min(budget, offered)
            occ = held / C if C else 0.0
            infl = 1.0 / max(0.02, 1.0 - occ)
            step_admit_ns[step] = PROSE_ADMIT_NS + mech.serialized_acquire_ns * infl
        elif early:
            # reservation holders = min(C, offered) (each built candidate reserves
            # a license, capped by the pool). Cold admissions also need a
            # reclaimable slot; early protection ties up held_start of the pool.
            held_start = min(C, offered)
            reclaimable_now = max(0, C - held_start)
            reservable = min(C, offered)
            max_admit = min(budget, reservable)
            # cold admissions limited by reclaimable slots
            cold_want = int(round(max_admit * cold_frac))
            cold_ok = min(cold_want, reclaimable_now)
            hot_ok = max_admit - cold_want
            admitted = max(0, hot_ok) + cold_ok
            held = min(C, offered)            # backlog reservations dominate
            occ = held / C if C else 0.0
            infl = 1.0 / max(0.02, 1.0 - occ)
            step_admit_ns[step] = PROSE_ADMIT_NS + mech.serialized_acquire_ns * infl
        else:
            # PROSE / GenOnly: hold nothing between steps, so the whole pool is
            # reclaimable; admit the full budget.
            max_admit = min(budget, offered)
            admitted = max_admit
            held = admitted                   # only in-flight transfers pinned
            step_admit_ns[step] = PROSE_ADMIT_NS
        step_admitted[step] = admitted
        step_held[step] = held

    pinned_peak = int(step_held.max()) if cfg.n_steps else 0
    pin_step_area = float(step_held.sum())
    reclaimable_area = float(np.clip(C - step_held, 0, None).sum())

    # Assign admission steps to request ids in build order (vectorized): the
    # first `step_admitted[s]` still-waiting requests built by step s are admitted
    # at s. Since the backlog is one step, a request built at step b is admitted
    # at b if it is within that step's admitted count, else it is superseded.
    admit_step = np.full(n, -1, dtype=np.int64)
    # requests are grouped contiguously by build step (per_step each)
    for step in range(cfg.n_steps):
        lo = step * per_step
        k = int(step_admitted[step])
        if k > 0:
            admit_step[lo:lo + k] = step

    admitted_mask = admit_step >= 0
    admit_step_eff = np.where(admitted_mask, admit_step, cfg.n_steps)
    backlog_wait = (admit_step_eff - build_step).astype(np.float64) * step_ns
    admit_proc_ns = np.where(
        admitted_mask,
        step_admit_ns[np.clip(admit_step, 0, cfg.n_steps - 1)],
        step_admit_ns.max(),
    )
    admission_wait = admit_proc_ns

    # ── bytes / RPE accounting ──
    requested_bytes = float(n) * obj_bytes
    n_admitted = int(admitted_mask.sum())
    race_admitted = races & admitted_mask
    evict_attempts = int(race_admitted.sum())
    if mech.protects_transfer:
        evict_blocked = evict_attempts
        evict_fired = 0
        valid_bytes = float(n_admitted) * obj_bytes
        stale_bytes = 0.0
        rpe_events = 0
    else:
        evict_blocked = 0
        evict_fired = evict_attempts
        # unprotected: a raced transfer loses its post-request tail to stale
        # (the reuse attempt lands mid-transfer; modeled at the midpoint, so
        # the tail is half the payload). An epoch fence (GENONLY_EF) defers
        # the slot overwrite by one grace period, so the transfer stays valid
        # for grace_ns beyond the request: the stale tail shrinks by
        # grace/service (and vanishes iff the fence outlives the transfer —
        # here grace=500 ns << service=16.6 us, so it does NOT vanish).
        tail = 0.5
        if mech.epoch_fence:
            tail = max(0.0, 0.5
                       - cfg.base.eviction_interval_ns / cfg.service_ns())
        valid_bytes = float(n_admitted) * obj_bytes - evict_fired * obj_bytes * tail
        stale_bytes = evict_fired * obj_bytes * tail
        rpe_events = evict_fired if tail > 0 else 0

    # ── snapshot-window staleness (REFCNT_S only; additive — every original
    #    mechanism has snapshot_check=True, so this block is inert for them) ──
    # Pin acquisition validates identity but NOT generation, so a reclaim in the
    # host-snapshot -> pin-acquire window means:
    #   * reincarnated (same id, new generation): the pin attaches to the WRONG
    #     incarnation -> STALE ADMIT. The consumer expects the old generation
    #     from byte 0, so the whole transferred payload is stale.
    #   * simply gone: the acquire FAILS and the host re-selects (one RETRY; the
    #     replacement is admitted in-step — the pool is never the constraint
    #     for a budget-sized pin set — but the descriptor pays one extra
    #     serialized round trip of admission latency).
    # The mid-transfer stream is untouched: REFCNT_S pins across the transfer,
    # so raced transfers are still blocked (protects_transfer=True above).
    stale_admits = 0
    retries = 0
    if subset and not mech.snapshot_check:
        snap = trace.race_snap & admitted_mask
        stale_mask = snap & trace.reincarn
        retry_mask = snap & ~trace.reincarn
        stale_admits = int(stale_mask.sum())
        retries = int(retry_mask.sum())
        valid_bytes -= stale_admits * obj_bytes
        stale_bytes += stale_admits * obj_bytes
        rpe_events += stale_admits
        admission_wait = admission_wait + retry_mask.astype(np.float64) * mech.serialized_acquire_ns

    # makespan: the admitted work drains over the decode horizon; add the
    # one-time serialized coordination fill for early-protection mechanisms.
    makespan = cfg.n_steps * step_ns + mech.serialized_acquire_ns
    pinned_byte_time = float(pin_step_area) * step_ns * obj_bytes
    reclaimable_mean = reclaimable_area / cfg.n_steps if cfg.n_steps else float(C)

    p50 = float(np.percentile(admission_wait, 50))
    p99 = float(np.percentile(admission_wait, 99))
    p999 = float(np.percentile(admission_wait, 99.9))
    backlog_p99 = float(np.percentile(backlog_wait, 99))
    blocked_ratio = evict_blocked / evict_attempts if evict_attempts else 0.0
    # blocked-reclaim ratio (the capacity story): fraction of the pool that is
    # non-reclaimable on average because it is tied up in held pins.
    nonreclaimable_frac = 1.0 - (reclaimable_area / cfg.n_steps / C if C else 0.0)

    return {
        "mechanism": mech.name,
        "label": mech.label,
        "oversubscription": cfg.oversubscription,
        "n_tenants": cfg.n_tenants,
        "bound_mode": cfg.bound_mode,
        "bound": C,
        "seed": cfg.seed,
        "n_requests": n,
        "makespan_ns": makespan,
        "valid_bytes": valid_bytes,
        "stale_bytes": stale_bytes,
        "requested_bytes": requested_bytes,
        "valid_throughput_Bpns": valid_bytes / makespan if makespan > 0 else 0.0,
        "rpe_events": rpe_events,
        "rpe_payload_frac": stale_bytes / max(1.0, valid_bytes + stale_bytes),
        "admission_p50_ns": p50,          # admission-PROCESSING latency (paper P99 metric)
        "admission_p99_ns": p99,
        "admission_p999_ns": p999,
        "backlog_wait_p99_ns": backlog_p99,   # end-to-end queue wait (diagnostic)
        "pinned_peak": pinned_peak,
        "pinned_byte_time_Bns": pinned_byte_time,
        "reclaimable_capacity_mean": reclaimable_mean,
        "reclaimable_capacity_frac": reclaimable_mean / C if C else 0.0,
        "nonreclaimable_frac": nonreclaimable_frac,
        "evict_attempts": evict_attempts,
        "evict_blocked": evict_blocked,
        "evict_fired": evict_fired,
        "blocked_reclaim_ratio": blocked_ratio,
        # design-space study metrics (exactly 0 for every original mechanism)
        "stale_admits": stale_admits,
        "stale_admit_rate": stale_admits / n_admitted if n_admitted else 0.0,
        "retries": retries,
        "retries_per_descriptor": retries / n_admitted if n_admitted else 0.0,
    }


# ════════════════════════════════════════════════════════════════════════════
# ENABLER-MATRIX REPLAY (ADDITIVE — 2x2 commit-gate x pool-autonomy experiment)
# ════════════════════════════════════════════════════════════════════════════
#
# The count-based model above has no notion of slot RESIDENCY (which object
# sits in which slot, when it goes dead, whether the endpoint may recycle the
# slot), so it cannot express "pool autonomy OFF" (slots are never recycled).
# This section adds exactly that, as a slot-resident per-step replay, reusing
# the SAME shared trace (generate_oversub_trace) so all four cells of the
# gate x autonomy matrix replay the identical candidate/race streams.
#
# THE TWO TOGGLES (everything else — candidate sets, 32-object admit budget,
# tenants, pool, oversubscription — is fixed across the four cells):
#
#   * commit-time gate (validate-and-hold at admission):
#       ON  -> an admitted transfer's slot is held for the transfer span; a
#              mid-transfer reuse attempt is BLOCKED (no stale payload).
#       OFF -> no hold; under autonomy the endpoint's own reuse activity can
#              recycle a slot mid-transfer -> stale payload (GENONLY-class
#              accounting: a raced transfer loses half its payload bytes,
#              giving RPE_payload ~ 17% at 32x, exactly as in replay_oversub).
#
#   * pool autonomy (endpoint autonomously evicts/reuses slots):
#       ON  -> a cold admission with no free slot reclaims one: a dead slot if
#              any, else an unpinned live slot. The pool therefore never
#              exhausts; reclaimable capacity ~= C - in-flight transfers.
#       OFF -> slots are NEVER recycled (no eviction, no reuse). Cold
#              admissions consume free slots until the pool is exhausted; from
#              then on only candidates that hit a still-LIVE resident object
#              can be admitted, and because residents keep dying (context
#              lifetime below), the pool fills with dead objects and sustained
#              valid throughput collapses. Reclaimable capacity = free slots
#              only -> ~0 after exhaustion. RPE == 0 in these cells only
#              because nothing is ever reused.
#
# OBJECT LIFETIME. A promoted object stays live for a geometric lifetime
# (mean ENABLER_LIFETIME_MEAN_STEPS decode steps) after admission — a context
# is read for a few decode steps and then goes dead. No refresh-on-read: with
# a churning serving workload the dead-object accumulation is the whole point
# of the autonomy-OFF column.

ENABLER_LIFETIME_MEAN_STEPS = 8.0   # mean live span of a promoted object (steps)


def replay_enabler_cell(trace: OversubTrace, gate_on: bool, autonomy_on: bool,
                        lifetime_mean_steps: float = ENABLER_LIFETIME_MEAN_STEPS
                        ) -> Dict:
    """Replay the shared offered-load trace for ONE cell of the enabler matrix.

    Returns per-run metrics plus per-step time series (valid payload bytes,
    stale payload bytes, reclaimable capacity fraction, admissions).
    """
    cfg = trace.cfg
    C = cfg.capacity
    budget = cfg.admit_budget
    per_step = cfg.oversubscription * budget
    n_steps = cfg.n_steps
    obj_bytes = float(cfg.base.object_bytes)
    p_die = 1.0 / max(1.0, lifetime_mean_steps)
    rng_life = np.random.default_rng(cfg.seed * 100003 + 17)

    # Slot accounting (counts + a residency map; individual slot ids unneeded).
    free = C
    dead = 0                                # dead objects occupying slots
    live: Dict[int, int] = {}               # obj_id -> death_step (exclusive)
    death_buckets: Dict[int, List[int]] = {}
    exhaustion_step = -1                    # first step a cold admission failed

    step_valid_bytes = np.zeros(n_steps, dtype=np.float64)
    step_stale_bytes = np.zeros(n_steps, dtype=np.float64)
    step_reclaimable = np.zeros(n_steps, dtype=np.float64)
    step_admitted = np.zeros(n_steps, dtype=np.int64)
    step_failed = np.zeros(n_steps, dtype=np.int64)
    n_admitted = 0
    rpe_events = 0
    evict_blocked = 0

    for s in range(n_steps):
        # expire residents whose lifetime ended before this step
        bucket = death_buckets.pop(s, None)
        if bucket:
            for o in bucket:
                if live.get(o) == s:
                    del live[o]
                    dead += 1

        lo = s * per_step
        cand = trace.obj_ids[lo:lo + per_step]
        raced = trace.race_xfer[lo:lo + per_step]

        admitted = 0
        failed = 0
        stale_events = 0
        if not autonomy_on and free == 0 and not live:
            # pool exhausted and nothing live: every candidate is a failed cold
            failed = per_step
            if exhaustion_step < 0:
                exhaustion_step = s
        else:
            admitted_objs = set()           # this step's in-flight objects
            for i in range(per_step):
                if admitted >= budget:
                    break
                o = int(cand[i])
                if o in live:
                    admitted += 1           # hit on a live-resident object
                else:
                    if not autonomy_on:
                        if free > 0:        # static pool: consume a fresh slot
                            free -= 1
                        else:               # exhausted: cold admission fails
                            failed += 1
                            if exhaustion_step < 0:
                                exhaustion_step = s
                            continue
                    else:                   # autonomy ON: reclaim a slot
                        if free > 0:
                            free -= 1
                        elif dead > 0:
                            dead -= 1
                        else:
                            # evict an unpinned live slot (earliest death
                            # first); this step's in-flight objects are held by
                            # the gate when gate_on, and are skipped either way
                            # so eviction pressure never double-counts the race
                            # stream below.
                            victim = min((k for k in live
                                          if k not in admitted_objs),
                                         key=lambda k: live[k], default=None)
                            if victim is None:  # all live slots in-flight: skip
                                continue
                            del live[victim]
                    death = s + int(rng_life.geometric(p_die))
                    live[o] = death
                    death_buckets.setdefault(death, []).append(o)
                    admitted += 1
                    admitted_objs.add(o)
                if raced[i] and autonomy_on:
                    # mid-transfer reuse attempt on this transfer's slot; only
                    # possible when the endpoint autonomously reuses slots. The
                    # gate blocks it (validate-and-hold); ungated it lands.
                    # With autonomy OFF nothing is ever reused, so no attempt
                    # can fire at all (RPE == 0 by construction there).
                    if gate_on:
                        evict_blocked += 1
                    else:
                        stale_events += 1

        step_admitted[s] = admitted
        step_failed[s] = failed
        n_admitted += admitted
        rpe_events += stale_events
        step_stale_bytes[s] = stale_events * 0.5 * obj_bytes
        step_valid_bytes[s] = admitted * obj_bytes - step_stale_bytes[s]
        if autonomy_on:
            # everything is reclaimable except this step's in-flight transfers
            step_reclaimable[s] = max(0.0, (C - admitted) / C) if C else 0.0
        else:
            # static pool: only never-used slots are reclaimable
            step_reclaimable[s] = free / C if C else 0.0

    valid_bytes = float(step_valid_bytes.sum())
    stale_bytes = float(step_stale_bytes.sum())
    sustain0 = n_steps // 2
    sustained = float(step_valid_bytes[sustain0:].mean()) if n_steps > 1 else valid_bytes
    return {
        "gate_on": bool(gate_on),
        "autonomy_on": bool(autonomy_on),
        "seed": cfg.seed,
        "oversubscription": cfg.oversubscription,
        "n_tenants": cfg.n_tenants,
        "capacity": C,
        "admit_budget": budget,
        "n_steps": n_steps,
        "valid_bytes": valid_bytes,
        "stale_bytes": stale_bytes,
        "n_admitted": n_admitted,
        "n_failed_admissions": int(step_failed.sum()),
        "rpe_events": rpe_events,
        "evict_blocked": evict_blocked,
        "rpe_payload_frac": stale_bytes / max(1.0, valid_bytes + stale_bytes),
        "sustained_valid_Bpstep": sustained,
        "mean_valid_Bpstep": float(step_valid_bytes.mean()) if n_steps else 0.0,
        "reclaimable_frac_mean": float(step_reclaimable.mean()) if n_steps else 0.0,
        "exhaustion_step": exhaustion_step,     # -1 = never exhausted
        "step_valid_bytes": step_valid_bytes,
        "step_stale_bytes": step_stale_bytes,
        "step_reclaimable_frac": step_reclaimable,
        "step_admitted": step_admitted,
    }
