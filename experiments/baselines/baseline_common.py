"""Shared backend for the mechanism-level baseline comparison.

This module defines ONE logical event trajectory per (workload, seed) and ONE
single-link discrete-event replay engine.  Every mechanism (NoCheck, shared
refcount, two-phase reservation, generation-only, generation-only + epoch
fence, RDMA-style key, segmented/cancelable DMA, PROSE) replays the *same*
trajectory through the *same* engine and the *same* CXL byte-level timing
model; only its protection / validation semantics differ.  This is what makes the comparison fair: the
promotion-arrival order, the endpoint scheduling order, the eviction/slot-reuse
decisions, and every random draw are generated once and shared verbatim.

Hardware timing is NOT re-invented here.  Link serialization, DRAM row
hit/miss, CXL.mem protocol processing and bridge transit are delegated to the
repository's calibrated :class:`CXLQueueConfig` / ``_service_time_ns`` (the same
model every other SimCXL-extension experiment uses).  This module adds only the
per-request *lifecycle* replay (queue -> admission check -> protection ->
segmented payload -> eviction race -> completion) that the aggregate
per-decode-step simulator cannot express.

See ``experiments/baselines/README.md`` for the per-mechanism state machine and
the exact point at which each protection is acquired and released.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simcxl_ext.cxl_queue_simulator import CXLQueueConfig  # noqa: E402

# Canonical segment sizes swept by the cancelable-DMA baseline (bytes).
SEGMENT_SIZES = [64, 256, 4096, 16384]

# Fixed method ordering used EVERYWHERE (raw rows, CSV, figure). Never rely on
# dict insertion / lexical order so the outputs are byte-stable across runs.
METHOD_ORDER = [
    "NoCheck",
    "SharedRef",
    "TwoPhase",
    "GenOnly",
    "GenOnlyEpochFence",
    "RDMAKey",
    "Segmented-64",
    "Segmented-256",
    "Segmented-4096",
    "Segmented-16384",
    "PROSE",
]

# Short labels for the figure (aligned with METHOD_ORDER).
# "NoCheck" is displayed as "Unsafe": it is the unsafe reference design (it
# streams payload without any residency validation, so it can expose stale
# bytes). Calling it "Unsafe" prevents readers from mistaking it for a sane
# optimization and makes ">1.0 normalized throughput" easy to explain — the
# unsafe design wastes link bandwidth on stale payload, so a correct mechanism's
# valid goodput can exceed it.
METHOD_LABELS = {
    "NoCheck": "Unsafe",
    "SharedRef": "RefCnt",
    "TwoPhase": "2Phase",
    "GenOnly": "GenOnly",
    "GenOnlyEpochFence": "GenOnly+EF",
    "RDMAKey": "RKey",
    "Segmented-64": "S64",
    "Segmented-256": "S256",
    "Segmented-4096": "S4K",
    "Segmented-16384": "S16K",
    "PROSE": "PROSE",
}


# ── Configuration ──────────────────────────────────────────────────────────
@dataclass
class BaselineConfig:
    """One experiment configuration (workload).

    Byte / latency constants default to the paper's calibrated CXL values; the
    driver overrides them from ``configs/baseline_sweep.yaml`` so nothing that
    matters is hard-coded in a plotting script.
    """
    name: str = "nominal"
    # ── request stream ──
    n_requests: int = 1200            # promotion descriptors per run
    object_bytes: int = 65536         # 64 KiB KV chunk (paper default)
    # endpoint slot cache (resident object table)
    endpoint_capacity: int = 256      # resident slots
    n_objects: int = 1024             # logical object universe (> capacity)
    # ── timing (ns) ── inter-arrival + endpoint scheduling ──
    # Arrival pacing is derived from the estimated per-request link service so
    # the endpoint runs at a controlled offered load (target_link_load); this
    # keeps the single-server queue STABLE (no runaway backlog) so pin-span and
    # makespan reflect a realistic near-saturated regime, not an artifact.
    target_link_load: float = 0.85         # offered load ρ on the endpoint link
    eviction_interval_ns: float = 500.0     # mean allocator reuse cadence
    # ── occupancy / race pressure ──
    target_occupancy: float = 0.70    # fraction of capacity kept resident
    hot_fraction: float = 0.25        # fraction of universe that is "hot"
    # ── link ──
    link_bw_gbps: float = 4.0         # per-endpoint effective CXL bandwidth
    # ── protocol / control byte costs (see README for provenance) ──
    descriptor_bytes: int = 64        # BDB descriptor / 64 B metadata summary
    completion_bytes: int = 64        # completion / doorbell writeback
    # shared-refcount metadata atomic (non-coherent path: flush+atomic+visibility)
    refcount_op_bytes: int = 64       # one cacheline RMW on shared metadata
    refcount_op_latency_ns: float = 250.0   # flush + atomic + visibility RTT
    # two-phase reservation exchange
    reserve_req_bytes: int = 64       # RESERVE(object_id, epoch)
    reserve_rsp_bytes: int = 32       # reservation token (HMAC-trunc)
    reserve_release_bytes: int = 32   # explicit release message
    reserve_rtt_ns: float = 3500.0    # host<->endpoint reserve round-trip
    # generation-only / rdma-key check cost at admission
    gen_check_latency_ns: float = 9.0     # endpoint epoch compare (== PROSE admit)
    # PROSE fused admit (epoch compare + pin install, one linearization point)
    prose_admit_latency_ns: float = 9.0
    # ── segmented DMA parameters ──
    per_segment_header_bytes: int = 16     # segment framing/alignment header
    per_segment_descriptor_bytes: int = 8  # per-segment sub-descriptor
    segment_check_latency_ns: float = 4.0  # re-check generation at segment commit
    max_inflight_segments: int = 1         # payload-commit pipeline depth
    payload_commit_depth: int = 1          # segments irrevocably in flight

    def cxl_config(self) -> CXLQueueConfig:
        """Build the shared byte-level CXL model at this workload's bandwidth."""
        cfg = CXLQueueConfig()
        cfg.bandwidth_gbps = self.link_bw_gbps
        cfg.raw_bandwidth_gbps = self.link_bw_gbps / 0.98
        cfg.chunk_size_bytes = self.object_bytes
        return cfg


# ── Mechanism behavioural specification ─────────────────────────────────────
@dataclass(frozen=True)
class MechanismSpec:
    """The *only* differences between mechanisms, captured declaratively.

    The replay engine (``replay_run``) is a single shared execution path;
    it consults this spec at exactly four decision points — descriptor
    generation, protection acquire, admission check, and per-segment commit —
    and nowhere else.  This guarantees no mechanism gets a private fast path.
    """
    name: str
    # protection acquired at host enqueue (blocks queue-time reclamation)
    #   None | "refcount" (TraCT) | "reserve" (two-phase)
    queue_protection: Optional[str] = None
    # admission-time validation bound to residency
    checks_epoch_at_admission: bool = False   # GenOnly / PROSE (and 2Phase reserve)
    checks_key_at_admission: bool = False      # RDMAKey / Segmented
    pins_at_admission: bool = False            # PROSE: fused epoch-check + pin
    # per-segment re-validation before irrevocable commit
    segment_bytes: Optional[int] = None        # Segmented-* only
    # coordination cost.
    #   extra_rtt              : COUNT of additional serialized host<->endpoint
    #                            request/response exchanges on the promotion
    #                            critical path (the "+RTT" column).
    #   serialized_acquire_ns  : wall-clock latency of the FIRST such exchange,
    #                            which fills the pipeline before the first payload
    #                            can start (added once to makespan in _summarize).
    #                            RefCnt = non-coherent metadata atomic latency;
    #                            2Phase = reserve round-trip; others = 0.
    extra_rtt: int = 0
    serialized_acquire_ns: float = 0.0
    # whether the mechanism holds protection spanning the payload transfer
    protects_transfer: bool = False            # RefCnt / 2Phase / PROSE
    # for the (b) matrix Queue-reclaim column
    queue_reclaim: str = "Y"                    # "Y" or "N"
    # Tigon-style epoch fence (GenOnlyEpochFence only): when the placement
    # authority reclaims a slot, the UNLINK (epoch bump + slot-key rotation —
    # what a fresh admission check observes) is immediate, exactly as under
    # GenOnly, but the slot OVERWRITE — the point at which an already-admitted
    # transfer's payload reads turn stale — is deferred by one grace period
    # (cfg.eviction_interval_ns, the allocator's own reuse epoch).
    epoch_fence: bool = False


SPECS: Dict[str, MechanismSpec] = {}


def register_spec(spec: MechanismSpec) -> MechanismSpec:
    """Register a mechanism spec into the global ordered registry."""
    SPECS[spec.name] = spec
    return spec


# ── Shared logical event trajectory ─────────────────────────────────────────
@dataclass
class Request:
    """One promotion descriptor in the shared trajectory (mechanism-agnostic).

    ``race_queue`` / ``race_xfer`` are *logical* race annotations: at replay the
    engine turns them into a real eviction (epoch bump + slot-key rotation) of
    this request's object at the annotated position of the queue / transfer
    window.  Because the annotation is relative (a fraction of the window), every
    mechanism experiences the identical logical race regardless of its own
    admit-latency, which is what keeps the comparison paired and fair.
    """
    request_id: int
    host_id: int
    object_id: int
    arrival_ns: float
    requested_bytes: int
    resident_at_arrival: bool
    race_queue: bool = False       # eviction fires while descriptor is queued
    race_xfer: bool = False        # eviction fires mid-payload-transfer
    race_frac: float = 0.5         # relative position of the eviction in window


@dataclass
class EventTrace:
    """The complete, mechanism-independent trajectory for one (workload, seed)."""
    workload: str
    seed: int
    config: BaselineConfig
    requests: List[Request]
    init_resident: List[int]       # object ids resident at t=0
    slot_of: Dict[int, int]        # object_id -> slot index at t=0


def generate_trace(config: BaselineConfig, workload: str, seed: int) -> EventTrace:
    """Generate ONE shared logical trajectory. All RNG lives here — replay is
    fully deterministic given this trace, so every mechanism replays identically.
    """
    rng = np.random.default_rng(seed)
    cap = config.endpoint_capacity
    n_resident = max(1, int(round(cap * config.target_occupancy)))

    # Hot object universe: requests concentrate on a hot set so the endpoint
    # cache is meaningfully contended.
    n_hot = max(1, int(round(config.n_objects * config.hot_fraction)))
    hot_objs = list(range(n_hot))

    # Initial residency: the first n_resident hot objects sit in slots.
    init_resident = hot_objs[:min(n_resident, n_hot)]
    slot_of = {obj: i for i, obj in enumerate(init_resident)}

    # Race pressure. In the race-stress workload the fraction is high AND biased
    # so that admission/check -> eviction/reuse -> transfer-completion windows
    # actually occur; the nominal workload keeps a modest background rate.
    if workload == "race_stress":
        p_race_xfer = 0.35     # eviction during transfer (post-check)
        p_race_queue = 0.15    # eviction while queued (pre-admission)
    else:
        p_race_xfer = 0.06
        p_race_queue = 0.03

    # Derive the mean inter-arrival from a target offered load on the link.
    # Nominal per-request service ≈ payload serialization + proto + DRAM setup.
    payload_serialize_ns = config.object_bytes / config.link_bw_gbps
    nominal_service_ns = payload_serialize_ns + (2 * 15.0 + 50.0) + 120.0
    mean_interarrival_ns = nominal_service_ns / max(1e-6, config.target_link_load)

    requests: List[Request] = []
    t = 0.0
    for i in range(config.n_requests):
        t += float(rng.exponential(mean_interarrival_ns))
        obj = int(hot_objs[rng.integers(0, n_hot)])
        r = rng.random()
        race_xfer = r < p_race_xfer
        race_queue = (not race_xfer) and (r < p_race_xfer + p_race_queue)
        requests.append(Request(
            request_id=i,
            host_id=int(rng.integers(0, 8)),
            object_id=obj,
            arrival_ns=t,
            requested_bytes=config.object_bytes,
            resident_at_arrival=True,   # freshly-promoted hot object is resident
            race_queue=race_queue,
            race_xfer=race_xfer,
            race_frac=float(rng.uniform(0.30, 0.70)),
        ))

    return EventTrace(
        workload=workload,
        seed=seed,
        config=config,
        requests=requests,
        init_resident=list(init_resident),
        slot_of=dict(slot_of),
    )


# ── Object / slot model shared by the replay ────────────────────────────────
class ObjectTable:
    """Resident-object metadata: epoch (generation), slot mapping, slot key,
    pin count.  An eviction bumps the epoch AND rotates the reused slot's key
    (RDMA-style generation capability), and can only pick a ``pin_count == 0``
    object.  This is the single source of truth the replay consults.
    """

    def __init__(self, trace: EventTrace):
        self.epoch: Dict[int, int] = {}
        self.slot: Dict[int, int] = dict(trace.slot_of)
        self.pin: Dict[int, int] = {}
        self.resident = set(trace.init_resident)
        self._next_key = 1
        self.slot_key: Dict[int, int] = {}
        self.rotations: List[Tuple[int, int]] = []   # (old_key, new_key)
        for obj in trace.init_resident:
            self.epoch[obj] = 0
            self.slot_key[self.slot[obj]] = self._fresh_key()

    def _fresh_key(self) -> int:
        k = self._next_key
        self._next_key += 1
        return k

    def ensure(self, obj: int, slot_hint: int) -> None:
        if obj not in self.epoch:
            self.epoch[obj] = 0
        if obj not in self.slot:
            self.slot[obj] = slot_hint
        if self.slot[obj] not in self.slot_key:
            self.slot_key[self.slot[obj]] = self._fresh_key()
        self.resident.add(obj)

    def evict_and_reuse(self, obj: int) -> Tuple[int, int]:
        """Evict ``obj`` and rotate its slot key for the next incarnation.

        Returns (old_slot_key, new_slot_key).  Refuses to evict a pinned object
        (endpoint autonomous-reclaim rule) by raising — callers gate on
        ``pin.get(obj, 0) == 0`` first.
        """
        assert self.pin.get(obj, 0) == 0, "eviction of a pinned object is illegal"
        sid = self.slot[obj]
        old_key = self.slot_key[sid]
        new_key = self._fresh_key()
        self.slot_key[sid] = new_key       # slot reused by a new logical object
        self.rotations.append((old_key, new_key))
        self.epoch[obj] = self.epoch.get(obj, 0) + 1   # generation bump
        self.resident.discard(obj)
        return old_key, new_key


# ── PROSE runtime invariant hook (Test 6) ───────────────────────────────────
class InvariantViolation(AssertionError):
    """Raised when PROSE's PAYLOAD_ISSUE invariant is violated at replay."""


# ── Single shared replay engine ─────────────────────────────────────────────
def replay_run(trace: EventTrace, spec: MechanismSpec,
               check_prose_invariant: bool = False) -> Dict:
    """Replay ONE shared trajectory under ONE mechanism.

    Single serialized endpoint+link timeline: descriptors are admitted in
    arrival order (endpoint dequeue), payload + control serialize on the link.
    Protection (refcount/reserve/pin) and validation (epoch/key) are the only
    mechanism-specific branches; they are read from ``spec``.

    Returns a dict with ``rows`` (per-request records) and ``summary``.
    """
    cfg = trace.config
    tbl = ObjectTable(trace)
    bytes_per_ns = cfg.link_bw_gbps            # GB/s == bytes/ns
    proto_ns = 2 * 15.0 + 50.0                 # SimCXL proto proc + bridge
    dram_setup_ns = 120.0                      # one DDR5 row-miss activation

    def serialize_ns(nbytes: float) -> float:
        return nbytes / bytes_per_ns if bytes_per_ns > 0 else 0.0

    ctx = _ReplayCtx(cfg, tbl, bytes_per_ns, proto_ns, dram_setup_ns,
                     serialize_ns, check_prose_invariant)

    rows: List[Dict] = []
    for req in trace.requests:
        obj = req.object_id
        tbl.ensure(obj, slot_hint=obj % cfg.endpoint_capacity)
        expected_epoch = tbl.epoch[obj]        # snapshot at descriptor generation
        expected_key = tbl.slot_key[tbl.slot[obj]]
        slot_id = tbl.slot[obj]

        rec = _new_record(req, spec, expected_epoch, expected_key, slot_id, cfg)
        _replay_one(req, spec, ctx, rec)
        rows.append(rec)

    return _summarize(trace, spec, rows, ctx.t_link, ctx)


@dataclass
class _ReplayCtx:
    """Mutable replay context threaded through ``_replay_one``."""
    cfg: BaselineConfig
    tbl: ObjectTable
    bytes_per_ns: float
    proto_ns: float
    dram_setup_ns: float
    serialize_ns: object                       # Callable[[float], float]
    check_prose_invariant: bool
    t_link: float = 0.0                        # serialized endpoint+link clock
    # eviction-ATTEMPT accounting (identical attempts across mechanisms; only the
    # blocked/fired split differs — that difference is exactly the retention
    # effect that lets a queue-pinning mechanism keep more objects transferable).
    evict_attempts_queue: int = 0              # queue-time attempts offered
    evict_attempts_xfer: int = 0              # transfer-time attempts offered
    evict_fired: int = 0                       # attempts that actually reclaimed
    evict_blocked: int = 0                     # attempts a pin blocked


def _new_record(req: Request, spec: MechanismSpec, expected_epoch: int,
                expected_key: int, slot_id: int, cfg: BaselineConfig) -> Dict:
    """Initialize the full request-level record (spec §V schema)."""
    return {
        "method": spec.name,
        "segment_bytes": spec.segment_bytes if spec.segment_bytes else 0,
        "host_id": req.host_id,
        "request_id": req.request_id,
        "object_id": req.object_id,
        "expected_epoch": expected_epoch,
        "observed_epoch_at_enqueue": expected_epoch,
        "observed_epoch_at_admission": expected_epoch,
        "slot_id": slot_id,
        "slot_key": expected_key,
        "object_bytes": cfg.object_bytes,
        "requested_bytes": req.requested_bytes,
        "valid_payload_bytes": 0,
        "stale_payload_bytes": 0,
        "wire_payload_bytes": 0,
        "control_bytes": 0,
        "header_bytes": 0,
        "descriptor_enqueue_ns": None,
        "protection_acquire_ns": None,
        "endpoint_admission_ns": None,
        "first_payload_issue_ns": None,
        "last_payload_complete_ns": None,
        "protection_release_ns": None,
        "reject_ns": None,
        "abort_ns": None,
        "extra_round_trips": spec.extra_rtt,
        "rpe_event": False,
        "reclaimed_while_queued": False,
    }


def _replay_one(req: Request, spec: MechanismSpec, ctx: _ReplayCtx,
                rec: Dict) -> None:
    """Replay a single request under one mechanism on the shared timeline.

    Timeline stages (all serialize on the single endpoint link clock t_link):
      1. host enqueue           -> descriptor_enqueue_ns, descriptor control byte
      2. (optional) protection  -> refcount RMW (host) or reserve RTT (endpoint)
      3. endpoint admission      -> epoch/key check bound (or not) to residency
      4. queue-time race         -> eviction while queued (pre-admission)
      5. payload transfer        -> segmented or whole; per-segment re-check
      6. transfer-time race      -> eviction mid-transfer (post-check)
      7. protection release / completion
    """
    cfg, tbl = ctx.cfg, ctx.tbl
    obj = req.object_id

    # ── 1. host enqueue (at the host arrival instant, NOT when the endpoint
    #      gets to it). This is the descriptor's logical birth; queue-time
    #      protection is acquired here, so protected mechanisms hold the object
    #      across the whole queue wait, not just the transfer. ──
    arrival = req.arrival_ns
    rec["descriptor_enqueue_ns"] = arrival
    rec["control_bytes"] += cfg.descriptor_bytes    # BDB descriptor on wire

    # ── 2. protection acquired at/near enqueue (queue-time protection) ──
    #    RefCnt: host atomically increments shared metadata BEFORE enqueue.
    #    2Phase: host completes a RESERVE round-trip; the token pins the object.
    if spec.queue_protection == "refcount":
        # non-coherent shared-metadata atomic: flush + atomic + visibility
        rec["protection_acquire_ns"] = arrival + cfg.refcount_op_latency_ns
        rec["control_bytes"] += cfg.refcount_op_bytes
        tbl.pin[obj] = tbl.pin.get(obj, 0) + 1      # refcount>0 blocks eviction
    elif spec.queue_protection == "reserve":
        # RESERVE(object_id, epoch): endpoint atomically checks residency+epoch,
        # installs a reservation pin, returns a unique token after one RTT. The
        # pin is held from token grant; the round-trip is pipelined ahead of the
        # payload (its net makespan cost is one RTT, added in _summarize).
        rec["protection_acquire_ns"] = arrival + cfg.reserve_rtt_ns
        rec["control_bytes"] += (cfg.reserve_req_bytes + cfg.reserve_rsp_bytes
                                 + cfg.reserve_release_bytes)
        tbl.pin[obj] = tbl.pin.get(obj, 0) + 1      # reservation pin held

    # ── endpoint service start: the single link serializes requests, so a
    #    descriptor waits in the endpoint queue until the link is free. ──
    service_start = max(ctx.t_link, arrival)

    # ── 4. queue-time eviction race (fires between enqueue and admission) ──
    # The endpoint may autonomously reclaim the object while the descriptor is
    # still queued IFF no queue-time protection holds a pin on it.
    if req.race_queue:
        ctx.evict_attempts_queue += 1          # SAME attempt offered to every mech
        if tbl.pin.get(obj, 0) == 0:
            # unprotected: endpoint reclaims the slot now (epoch bump + key rot)
            tbl.evict_and_reuse(obj)
            rec["reclaimed_while_queued"] = True
            ctx.evict_fired += 1
        else:
            # RefCnt / 2Phase pinned it: reclamation is BLOCKED (Q-reclaim = N).
            # The object stays resident and transferable — this retention is why
            # a queue-pinning mechanism can post higher valid throughput.
            rec["reclaimed_while_queued"] = False
            ctx.evict_blocked += 1

    # ── 3. endpoint admission check ──
    t = service_start + _endpoint_admit_latency(spec, cfg)
    rec["endpoint_admission_ns"] = t
    rec["observed_epoch_at_admission"] = tbl.epoch.get(obj, 0)
    cur_key = tbl.slot_key.get(tbl.slot.get(obj, -1), -1)

    epoch_ok = (tbl.epoch.get(obj, 0) == rec["expected_epoch"]) and (obj in tbl.resident)
    key_ok = (cur_key == rec["slot_key"]) and (obj in tbl.resident)

    # Admission verdict per mechanism.
    admitted = True
    if spec.checks_epoch_at_admission and not epoch_ok:
        admitted = False
    if spec.checks_key_at_admission and not key_ok:
        admitted = False

    if not admitted:
        # rejected at admission: NO payload issued -> zero stale, zero RPE.
        rec["reject_ns"] = t
        rec["control_bytes"] += cfg.completion_bytes   # null completion
        _release_protection(spec, tbl, obj, rec, t)
        rec["last_payload_complete_ns"] = None
        ctx.t_link = t
        return

    # PROSE fuses epoch validation + pin acquisition at ONE linearization point.
    if spec.pins_at_admission:
        tbl.pin[obj] = tbl.pin.get(obj, 0) + 1
        rec["protection_acquire_ns"] = t

    _do_transfer(req, spec, ctx, rec, t, epoch_ok, key_ok)


def _endpoint_admit_latency(spec: MechanismSpec, cfg: BaselineConfig) -> float:
    """Endpoint decision latency at admission (ns)."""
    if spec.pins_at_admission:
        return cfg.prose_admit_latency_ns          # fused epoch-check + pin
    if spec.checks_epoch_at_admission or spec.checks_key_at_admission:
        return cfg.gen_check_latency_ns            # one-time epoch/key compare
    return 0.0                                      # NoCheck: no decision


def _release_protection(spec: MechanismSpec, tbl: "ObjectTable", obj: int,
                        rec: Dict, t: float) -> None:
    """Drop any pin/refcount/reservation this mechanism holds on ``obj``."""
    if spec.queue_protection in ("refcount", "reserve") or spec.pins_at_admission:
        if tbl.pin.get(obj, 0) > 0:
            tbl.pin[obj] -= 1
        rec["protection_release_ns"] = t


def _do_transfer(req: Request, spec: MechanismSpec, ctx: _ReplayCtx, rec: Dict,
                 t: float, epoch_ok: bool, key_ok: bool) -> None:
    """Stages 5-7: issue payload (segmented or whole), resolve the mid-transfer
    eviction race, account valid/stale bytes, release protection.

    Physical model of the transfer-time race (the crux of the comparison):
      * If a pin protects the transfer (RefCnt / 2Phase / PROSE) the endpoint
        MAY NOT reclaim the object mid-transfer -> the race is a no-op, every
        byte is valid, stale == 0.
      * Otherwise the eviction fires at byte offset ``race_frac * object_bytes``.
        Bytes issued strictly before that offset read the authorized generation
        (valid); bytes issued after it read the reused slot (STALE).
      * A single non-preemptible transfer (GenOnly, RDMAKey single-check) cannot
        stop, so ALL post-eviction bytes are stale.
      * A cancelable/segmented transfer re-checks the slot key before dispatching
        each new segment; once the key rotates it stops.  But up to
        ``max_inflight_segments`` segments are already in the irrevocable
        pipeline and complete as stale, bounding waste by
        ``segment_bytes * max_inflight_segments``.
    """
    cfg, tbl = ctx.cfg, ctx.tbl
    obj = req.object_id
    total_bytes = req.requested_bytes
    seg = spec.segment_bytes or total_bytes
    n_segments = max(1, math.ceil(total_bytes / seg))
    protected = spec.protects_transfer and tbl.pin.get(obj, 0) > 0
    inflight = max(1, cfg.max_inflight_segments)

    # Is the object ALREADY on the wrong generation at transfer start?  This
    # happens when a queue-time eviction reused the slot and the mechanism did
    # not reject at admission (NoCheck).  Then every payload byte is stale.
    already_stale = (tbl.epoch.get(obj, 0) != rec["expected_epoch"]
                     or tbl.slot_key.get(tbl.slot.get(obj, -1), -2) != rec["slot_key"])

    # eviction byte offset (None if no transfer race or the pin blocks it)
    if already_stale:
        evict_offset = 0.0
        evict_seg = 0
        evicted_flag_start = True
    elif req.race_xfer and not protected:
        ctx.evict_attempts_xfer += 1           # offered attempt that WILL fire
        evict_offset = req.race_frac * total_bytes
        evict_seg = int(evict_offset // seg)
        evicted_flag_start = False
    else:
        if req.race_xfer and protected:
            # SAME transfer-time attempt is offered, but the pin blocks it: the
            # transfer completes entirely valid. This is the transfer-scoped
            # retention that RefCnt / 2Phase / PROSE all share.
            ctx.evict_attempts_xfer += 1
            ctx.evict_blocked += 1
        evict_offset = None
        evict_seg = -1
        evicted_flag_start = False

    # Stale byte boundary. Normally identical to ``evict_offset``: bytes issued
    # after the reclaim request read the reused slot. Under an epoch fence
    # (GenOnlyEpochFence) the slot OVERWRITE is deferred by one grace period
    # (cfg.eviction_interval_ns — the allocator's own reuse epoch), so bytes
    # issued during the grace window still read the authorized generation and
    # the stale boundary moves downstream by grace_ns * bytes_per_ns; if the
    # grace window covers the rest of the transfer, this race exposes nothing.
    # The UNLINK itself (epoch bump + key rotation, fired below at
    # ``evict_offset``) is NOT deferred: a descriptor checked after the request
    # rejects exactly as under GenOnly, so the fence can only shrink exposure.
    # (``already_stale`` keeps a zero boundary: that reclaim's grace period has
    # long elapsed by the time this transfer starts.)
    if spec.epoch_fence and evict_offset is not None and not already_stale:
        stale_offset = min(total_bytes, evict_offset
                           + cfg.eviction_interval_ns * ctx.bytes_per_ns)
    else:
        stale_offset = evict_offset

    rec["first_payload_issue_ns"] = t
    valid_bytes = 0.0
    stale_bytes = 0.0
    evicted = evicted_flag_start

    for s in range(n_segments):
        seg_start = s * seg
        seg_bytes = min(seg, total_bytes - seg_start)

        # ── cancelable/segmented: re-check generation before a NEW segment ──
        # The check catches the rotated key once the invalidation is visible AND
        # this segment is beyond the in-flight window that was already committed.
        if spec.segment_bytes is not None and s > 0:
            t += cfg.segment_check_latency_ns
            if evicted and (s - evict_seg) >= inflight:
                rec["abort_ns"] = t         # cancel: issue no further segments
                break

        # ── PROSE runtime invariant at every payload issue ──
        if ctx.check_prose_invariant and spec.pins_at_admission:
            resident = obj in tbl.resident
            epoch_match = tbl.epoch.get(obj, 0) == rec["expected_epoch"]
            pinned = tbl.pin.get(obj, 0) > 0
            if not (resident and epoch_match and pinned):
                raise InvariantViolation(
                    f"PROSE PAYLOAD_ISSUE invariant violated: req={req.request_id}"
                    f" obj={obj} resident={resident} epoch_match={epoch_match}"
                    f" pinned={pinned} seg={s}")

        # per-segment header + sub-descriptor overhead for each ISSUED segment
        if spec.segment_bytes is not None:
            rec["header_bytes"] += cfg.per_segment_header_bytes
            rec["control_bytes"] += cfg.per_segment_descriptor_bytes

        # ── fire the eviction the instant this segment crosses the offset ──
        if (evict_offset is not None and not evicted
                and seg_start + seg_bytes > evict_offset):
            if tbl.pin.get(obj, 0) == 0:
                tbl.evict_and_reuse(obj)    # epoch bump + slot-key rotation
                evicted = True
                ctx.evict_fired += 1

        # commit this segment to the irrevocable pipeline (serialize on link)
        t += seg_bytes / ctx.bytes_per_ns
        if s == 0:
            t += ctx.proto_ns + ctx.dram_setup_ns
        rec["wire_payload_bytes"] += seg_bytes

        # split the segment at the stale boundary: pre-boundary valid, post stale
        if stale_offset is None or seg_start + seg_bytes <= stale_offset:
            valid_bytes += seg_bytes
        elif seg_start >= stale_offset:
            stale_bytes += seg_bytes
        else:
            v = stale_offset - seg_start
            valid_bytes += v
            stale_bytes += seg_bytes - v

    rec["valid_payload_bytes"] = int(round(valid_bytes))
    rec["stale_payload_bytes"] = int(round(stale_bytes))
    rec["control_bytes"] += cfg.completion_bytes       # completion writeback
    rec["last_payload_complete_ns"] = t
    if stale_bytes > 0:
        rec["rpe_event"] = True

    _release_protection(spec, tbl, obj, rec, t)
    ctx.t_link = t


def _pin_span_ratio(rec: Dict) -> Optional[float]:
    """protection span / payload span for one successful, protected transfer."""
    pa = rec["protection_acquire_ns"]
    pr = rec["protection_release_ns"]
    fp = rec["first_payload_issue_ns"]
    lp = rec["last_payload_complete_ns"]
    if pa is None or pr is None or fp is None or lp is None:
        return None
    payload_span = lp - fp
    if payload_span <= 0:
        return None
    return (pr - pa) / payload_span


def _summarize(trace: EventTrace, spec: MechanismSpec, rows: List[Dict],
               makespan_ns: float, ctx: "_ReplayCtx") -> Dict:
    """Reduce per-request rows to the run-level summary (spec §V run fields)."""
    # The first serialized coordination exchange enters the makespan once as
    # pipeline fill: the first payload cannot start until that exchange returns.
    # Subsequent exchanges pipeline behind in-flight transfers, so the
    # steady-state throughput cost is this single fill (the tail/pin-span cost is
    # reported separately). The latency charged is the mechanism's OWN acquire
    # cost (RefCnt: metadata-atomic latency; 2Phase: reserve RTT), NOT a shared
    # constant. Mechanisms with serialized_acquire_ns == 0 are unaffected.
    makespan_ns = makespan_ns + spec.serialized_acquire_ns
    total_requested = sum(r["requested_bytes"] for r in rows)
    total_valid = sum(r["valid_payload_bytes"] for r in rows)
    total_stale = sum(r["stale_payload_bytes"] for r in rows)
    total_wire = sum(r["wire_payload_bytes"] for r in rows)
    total_control = sum(r["control_bytes"] for r in rows)
    total_header = sum(r["header_bytes"] for r in rows)
    completed = [r for r in rows if r["last_payload_complete_ns"] is not None
                 and r["reject_ns"] is None]
    rejected = [r for r in rows if r["reject_ns"] is not None]
    aborted = [r for r in rows if r["abort_ns"] is not None]
    rpe_events = sum(1 for r in rows if r["rpe_event"])

    pin_spans = [x for x in (_pin_span_ratio(r) for r in completed) if x is not None]

    return {
        "workload": trace.workload,
        "seed": trace.seed,
        "method": spec.name,
        "segment_bytes": spec.segment_bytes or 0,
        "extra_rtt": spec.extra_rtt,
        "queue_reclaim": spec.queue_reclaim,
        "makespan_ns": makespan_ns,
        "total_requested_bytes": total_requested,
        "total_valid_bytes": total_valid,
        "total_stale_bytes": total_stale,
        "total_wire_bytes": total_wire,
        "total_control_bytes": total_control,
        "total_header_bytes": total_header,
        "completed_valid_requests": len(completed),
        "rejected_requests": len(rejected),
        "aborted_requests": len(aborted),
        "rpe_events": rpe_events,
        "pin_span_ratio_median": float(np.median(pin_spans)) if pin_spans else 0.0,
        "pin_span_ratio_p95": float(np.percentile(pin_spans, 95)) if pin_spans else 0.0,
        "serialized_acquire_ns": spec.serialized_acquire_ns,
        # eviction-attempt bookkeeping (identical attempts; blocked/fired differ)
        "evict_attempts_queue": ctx.evict_attempts_queue,
        "evict_attempts_xfer": ctx.evict_attempts_xfer,
        "evict_attempts_total": ctx.evict_attempts_queue + ctx.evict_attempts_xfer,
        "evict_fired": ctx.evict_fired,
        "evict_blocked": ctx.evict_blocked,
        "n_requests": len(rows),
        "rows": rows,
    }


def valid_throughput(summary: Dict) -> float:
    """valid_payload_bytes / makespan  (bytes per ns == GB/s)."""
    mk = summary["makespan_ns"]
    return summary["total_valid_bytes"] / mk if mk > 0 else 0.0


def control_header_overhead_pct(summary: Dict) -> float:
    """(control + header) / wire * 100."""
    wire = summary["total_wire_bytes"]
    if wire <= 0:
        return 0.0
    return (summary["total_control_bytes"] + summary["total_header_bytes"]) / wire * 100.0


def stale_mib_per_gib(summary: Dict) -> float:
    """total_stale_bytes / total_requested_bytes * 1024  (MiB stale / GiB req)."""
    req = summary["total_requested_bytes"]
    if req <= 0:
        return 0.0
    return summary["total_stale_bytes"] / req * 1024.0

