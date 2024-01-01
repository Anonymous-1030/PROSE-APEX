#!/usr/bin/env python3
"""Correctness tests for the seven-mechanism baseline comparison (spec §VII).

These do NOT rely on the final figure. Each test constructs a controlled
trajectory (forced event orderings) and asserts the mechanism-level invariants
that make the comparison meaningful:

  Test 1  no eviction            -> every mechanism stale==0
  Test 2  check-then-evict race  -> GenOnly/RDMAKey/large-seg stale>0;
                                     Segmented bounded; RefCnt/2Phase/PROSE==0
  Test 3  queue-time eviction    -> RefCnt/2Phase block reclaim; PROSE rejects
                                     (no payload) yet allows reclaim
  Test 4  long transfer          -> one admission check cannot protect the
                                     whole lifetime (GenOnly/RDMAKey leak)
  Test 5  segment upper bound    -> waste <= segment_bytes*max_inflight_segments
  Test 6  PROSE invariant        -> holds at every payload issue (no raise)
  Test 7  slot-key rotation      -> reused slot gets a fresh key; old capability
                                     does not re-validate
  Test 8  epoch fence            -> deferred overwrite shrinks the stale tail by
                                     grace*bandwidth; queue race == GenOnly;
                                     0 < fence <= GenOnly on paired traces
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import experiments.baselines as B  # noqa: E402  (registers specs)
from experiments.baselines.baseline_common import (  # noqa: E402
    BaselineConfig, EventTrace, Request, ObjectTable, replay_run,
    generate_trace, SPECS, SEGMENT_SIZES, InvariantViolation,
)


# ── helpers ─────────────────────────────────────────────────────────────────
def _cfg(**kw) -> BaselineConfig:
    base = dict(object_bytes=65536, endpoint_capacity=8, n_objects=32,
                n_requests=1, link_bw_gbps=4.0, max_inflight_segments=1,
                payload_commit_depth=1)
    base.update(kw)
    return BaselineConfig(**base)


def _single_trace(cfg: BaselineConfig, *, race_queue=False, race_xfer=False,
                  race_frac=0.5, obj=0, arrival=0.0) -> EventTrace:
    """One request against a resident object, with forced race annotations."""
    req = Request(request_id=0, host_id=0, object_id=obj, arrival_ns=arrival,
                  requested_bytes=cfg.object_bytes, resident_at_arrival=True,
                  race_queue=race_queue, race_xfer=race_xfer, race_frac=race_frac)
    return EventTrace(workload="unit", seed=0, config=cfg, requests=[req],
                      init_resident=[obj], slot_of={obj: 0})


ALL = list(B.ordered_specs())
SEG = [SPECS[f"Segmented-{s}"] for s in SEGMENT_SIZES]
PROTECTED = [SPECS["SharedRef"], SPECS["TwoPhase"], SPECS["PROSE"]]


# ── Test 1: no eviction => zero stale for every mechanism ────────────────────
def test_1_no_eviction_zero_stale():
    cfg = _cfg()
    tr = _single_trace(cfg, race_queue=False, race_xfer=False)
    for spec in ALL:
        s = replay_run(tr, spec)
        assert s["total_stale_bytes"] == 0, f"{spec.name} leaked without eviction"
        # a clean request must actually move the payload
        assert s["total_valid_bytes"] == cfg.object_bytes, spec.name


# ── Test 2: check succeeds, then eviction+reuse before transfer completes ────
def test_2_check_then_evict_midtransfer():
    cfg = _cfg()
    tr = _single_trace(cfg, race_xfer=True, race_frac=0.5)
    stale = {sp.name: replay_run(tr, sp)["total_stale_bytes"] for sp in ALL}

    # unprotected single-check mechanisms leak the post-eviction bytes
    assert stale["GenOnly"] > 0
    assert stale["RDMAKey"] > 0
    # protected mechanisms: the pin blocks the mid-transfer eviction -> zero
    assert stale["SharedRef"] == 0
    assert stale["TwoPhase"] == 0
    assert stale["PROSE"] == 0
    # segmented: bounded (see Test 5); strictly less than the single-check leak
    for s in SEGMENT_SIZES:
        assert stale[f"Segmented-{s}"] <= s * cfg.max_inflight_segments
        assert stale[f"Segmented-{s}"] <= stale["GenOnly"]


# ── Test 3: eviction while the descriptor is still queued ────────────────────
def test_3_queue_time_eviction():
    cfg = _cfg()
    tr = _single_trace(cfg, race_queue=True)
    rows = {sp.name: replay_run(tr, sp) for sp in ALL}

    # RefCnt and TwoPhase pin at enqueue -> object NOT reclaimable while queued
    for name in ("SharedRef", "TwoPhase"):
        rec = rows[name]["rows"][0]
        assert rec["reclaimed_while_queued"] is False, name
        assert rows[name]["total_stale_bytes"] == 0, name

    # PROSE does NOT pin while queued -> reclamation allowed, and the now-stale
    # descriptor is REJECTED at admission (no payload, no stale bytes).
    prose = rows["PROSE"]
    prec = prose["rows"][0]
    assert prec["reclaimed_while_queued"] is True
    assert prose["rejected_requests"] == 1
    assert prose["total_stale_bytes"] == 0
    assert prose["total_wire_bytes"] == 0   # PROSE issues no payload on reject


# ── Test 4: long transfer — one validation cannot protect the lifetime ───────
def test_4_long_transfer_single_check_insufficient():
    # very large object so transfer time >> admission check latency
    cfg = _cfg(object_bytes=4 * 1024 * 1024)
    tr = _single_trace(cfg, race_xfer=True, race_frac=0.5)
    for name in ("GenOnly", "RDMAKey"):
        s = replay_run(tr, SPECS[name])
        assert s["total_stale_bytes"] > 0, f"{name} should leak on a long xfer"
        # roughly the post-eviction half of the object
        assert s["total_stale_bytes"] >= cfg.object_bytes * 0.25
    # PROSE still protects the whole lifetime
    assert replay_run(tr, SPECS["PROSE"])["total_stale_bytes"] == 0


# ── Test 5: segmented waste upper bound ──────────────────────────────────────
@pytest.mark.parametrize("seg", SEGMENT_SIZES)
@pytest.mark.parametrize("inflight", [1, 2, 4])
def test_5_segment_upper_bound(seg, inflight):
    cfg = _cfg(object_bytes=1024 * 1024, max_inflight_segments=inflight,
               payload_commit_depth=inflight)
    for frac in (0.2, 0.5, 0.8):
        tr = _single_trace(cfg, race_xfer=True, race_frac=frac)
        spec = SPECS[f"Segmented-{seg}"]
        s = replay_run(tr, spec)
        assert s["total_stale_bytes"] <= seg * inflight, (
            f"seg={seg} inflight={inflight} frac={frac} "
            f"stale={s['total_stale_bytes']}")


# ── Test 6: PROSE runtime invariant holds at every payload issue ─────────────
def test_6_prose_invariant_holds():
    # exercise both nominal and heavy-race trajectories with the checker ON
    for wl in ("nominal", "race_stress"):
        cfg = BaselineConfig(name=wl, n_requests=400, endpoint_capacity=64,
                             n_objects=256)
        tr = generate_trace(cfg, wl, seed=7)
        # must NOT raise InvariantViolation
        s = replay_run(tr, SPECS["PROSE"], check_prose_invariant=True)
        assert s["total_stale_bytes"] == 0


def test_6_prose_invariant_catches_violation():
    """Sanity: the checker is not vacuous. A corrupted 'PROSE' that issues
    payload beats WITHOUT protecting the transfer must trip the invariant.

    We corrupt PROSE by DROPPING its epoch check (checks_epoch_at_admission
    False) while keeping the pin. With a queue-time eviction the descriptor
    goes stale before admission; the corrupted gate fails to reject it, pins,
    and issues payload on a bumped generation -> resident/epoch check fails ->
    InvariantViolation. Real PROSE (with the epoch check) rejects instead.
    """
    cfg = _cfg()
    tr = _single_trace(cfg, race_queue=True)
    broken = replace(SPECS["PROSE"], checks_epoch_at_admission=False)
    with pytest.raises(InvariantViolation):
        replay_run(tr, broken, check_prose_invariant=True)


# ── Test 8: epoch fence — deferred overwrite shrinks but never eliminates ────
def test_8_epoch_fence_transfer_race_shrinks_tail():
    """Mid-transfer race: the fence defers the slot OVERWRITE by one grace
    period, so the stale tail shrinks by exactly grace * bandwidth and those
    bytes are credited as valid. The reclaim still fires (deferred, not
    blocked) and the same RPE events occur."""
    cfg = _cfg()                     # grace = eviction_interval_ns = 500 ns
    tr = _single_trace(cfg, race_xfer=True, race_frac=0.5)
    g = replay_run(tr, SPECS["GenOnly"])
    f = replay_run(tr, SPECS["GenOnlyEpochFence"])
    grace_bytes = int(cfg.eviction_interval_ns * cfg.link_bw_gbps)   # 2000 B
    assert g["total_stale_bytes"] == cfg.object_bytes // 2
    assert f["total_stale_bytes"] == g["total_stale_bytes"] - grace_bytes
    assert f["total_valid_bytes"] == g["total_valid_bytes"] + grace_bytes
    assert f["evict_fired"] == g["evict_fired"]
    assert f["rpe_events"] == g["rpe_events"]


def test_8_epoch_fence_queue_race_behaves_like_genonly():
    """Queue-time race: the UNLINK is immediate, so a descriptor checked after
    the reclaim request rejects exactly as under GenOnly (no payload, no
    stale). The fence can only shrink exposure, never enlarge it."""
    cfg = _cfg()
    tr = _single_trace(cfg, race_queue=True)
    g = replay_run(tr, SPECS["GenOnly"])
    f = replay_run(tr, SPECS["GenOnlyEpochFence"])
    assert f["rejected_requests"] == g["rejected_requests"] == 1
    assert f["total_stale_bytes"] == g["total_stale_bytes"] == 0
    assert f["rows"][0]["reclaimed_while_queued"] is True


def test_8_epoch_fence_bounded_by_genonly_on_paired_traces():
    """Across generated trajectories: 0 < fence stale < GenOnly stale — the
    fence helps (deferred overwrite) but does not protect (no transfer-span
    pin), and the grace period is shorter than the transfer tail."""
    for wl in ("nominal", "race_stress"):
        cfg = BaselineConfig(name=wl, n_requests=400, endpoint_capacity=64,
                             n_objects=256)
        tr = generate_trace(cfg, wl, seed=7)
        g = replay_run(tr, SPECS["GenOnly"])
        f = replay_run(tr, SPECS["GenOnlyEpochFence"])
        assert 0 < f["total_stale_bytes"] < g["total_stale_bytes"], wl


def test_8_epoch_fence_longer_than_transfer_zeroes_stale():
    """Boundary check: a grace period covering the whole transfer window would
    zero the exposure — that regime is NOT the measured one (grace = one
    allocator epoch << transfer window); it exists here to pin down the
    semantics, not as a reported operating point."""
    cfg = _cfg(eviction_interval_ns=1e6)     # 1 ms >> 16.4 us transfer window
    tr = _single_trace(cfg, race_xfer=True, race_frac=0.3)
    f = replay_run(tr, SPECS["GenOnlyEpochFence"])
    assert f["total_stale_bytes"] == 0


# ── Test 7: slot-key rotation on reuse; old capability does not revalidate ───
def test_7_slot_key_rotation():
    cfg = _cfg()
    tr = _single_trace(cfg, obj=0)
    tbl = ObjectTable(tr)
    old_key = tbl.slot_key[tbl.slot[0]]
    tbl.pin[0] = 0
    old, new = tbl.evict_and_reuse(0)
    assert old == old_key
    assert new != old_key, "slot key must rotate on reuse"
    assert tbl.slot_key[tbl.slot[0]] == new
    # old capability (old_key) no longer matches the current slot key
    assert tbl.slot_key[tbl.slot[0]] != old_key


def test_7_rotation_blocks_pinned():
    """A pinned object may never be evicted/rotated (endpoint reclaim rule)."""
    cfg = _cfg()
    tr = _single_trace(cfg, obj=0)
    tbl = ObjectTable(tr)
    tbl.pin[0] = 1
    with pytest.raises(AssertionError):
        tbl.evict_and_reuse(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
