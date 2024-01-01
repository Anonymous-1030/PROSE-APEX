"""Shared trace-ingestion + measurement helpers for the rebuttal experiments.

These helpers MEASURE properties from an actual descriptor trace (the JSONL
format produced by scripts/gen_causal_trace.py and scripts/collect_real_trace.py).
Nothing here hard-codes the paper's headline numbers (0.52 overlap, 0.65
Jaccard, 14.4% RPE) — every quantity is computed from the trace stream, so the
returned value is whatever the trace actually contains.

Trace line format (one JSON object per tenant per step):
  {"step": int, "tenant_id": int,
   "descriptors": [{"chunk_id": int, "priority": int, "epoch": int}, ...]}

Used by:
  * run_mechanism_ablation.py   (B) — CFO dedup channel is driven by the real
                                       CFOCoalesceModel over these requests.
  * run_trace_sensitivity.py    (D) — overlap / Jaccard / RPE-residual are all
                                       measured, not parameterized.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np


# --------------------------------------------------------------------------- #
# Trace container                                                             #
# --------------------------------------------------------------------------- #
@dataclass
class Trace:
    """A decode trace grouped by step.

    steps[t] maps tenant_id -> list[chunk_id] (the tenant's working set at step t).
    prio[t]  maps tenant_id -> {chunk_id: priority} for that step.
    """
    steps: List[Dict[int, List[int]]]
    prio: List[Dict[int, Dict[int, int]]]
    n_tenants: int
    name: str = "trace"

    @property
    def n_steps(self) -> int:
        return len(self.steps)


def load_trace(path: str | Path, name: Optional[str] = None,
               max_steps: Optional[int] = None) -> Trace:
    """Load a JSONL descriptor trace, grouped by step.

    Robust to line ordering: records are bucketed by their "step" field, not by
    file position. A record missing required keys is skipped.
    """
    path = Path(path)
    by_step: Dict[int, Dict[int, List[int]]] = {}
    by_step_prio: Dict[int, Dict[int, Dict[int, int]]] = {}
    tenants: set[int] = set()

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "step" not in rec or "tenant_id" not in rec:
                continue
            step = int(rec["step"])
            tid = int(rec["tenant_id"])
            descs = rec.get("descriptors", [])
            cids = [int(d["chunk_id"]) for d in descs if "chunk_id" in d]
            pri = {int(d["chunk_id"]): int(d.get("priority", 1))
                   for d in descs if "chunk_id" in d}
            by_step.setdefault(step, {})[tid] = cids
            by_step_prio.setdefault(step, {})[tid] = pri
            tenants.add(tid)

    ordered = sorted(by_step.keys())
    if max_steps is not None:
        ordered = ordered[:max_steps]
    steps = [by_step[s] for s in ordered]
    prio = [by_step_prio[s] for s in ordered]
    return Trace(steps=steps, prio=prio, n_tenants=len(tenants),
                 name=name or path.stem)


# --------------------------------------------------------------------------- #
# Measurements (all derived from the trace — never hard-coded)                #
# --------------------------------------------------------------------------- #
def measure_inter_tenant_overlap(trace: Trace) -> Dict[str, float]:
    """Mean pairwise inter-tenant working-set overlap per step.

    Overlap for a tenant pair (a, b) at step t is the symmetric fraction
        |WS_a ∩ WS_b| / min(|WS_a|, |WS_b|)
    which is the fraction of the smaller working set that is shared — this is
    the quantity CFO can coalesce. We average over all tenant pairs and steps,
    and also return the distribution spread.

    Returns measured mean / p5 / p50 / p95, NOT the trace's generation target.
    """
    per_step_means: List[float] = []
    all_pairs: List[float] = []
    for ws in trace.steps:
        tids = sorted(ws.keys())
        sets = {t: set(ws[t]) for t in tids}
        pair_vals: List[float] = []
        for i in range(len(tids)):
            for j in range(i + 1, len(tids)):
                a, b = sets[tids[i]], sets[tids[j]]
                denom = min(len(a), len(b))
                if denom == 0:
                    continue
                ov = len(a & b) / denom
                pair_vals.append(ov)
                all_pairs.append(ov)
        if pair_vals:
            per_step_means.append(float(np.mean(pair_vals)))
    if not all_pairs:
        return {"mean": 0.0, "p5": 0.0, "p50": 0.0, "p95": 0.0, "n": 0}
    arr = np.array(all_pairs)
    return {
        "mean": float(arr.mean()),
        "p5": float(np.percentile(arr, 5)),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "step_mean": float(np.mean(per_step_means)),
        "n": int(arr.size),
    }


def measure_jaccard_selfcorr(trace: Trace) -> Dict[str, float]:
    """Per-tenant step-to-step Jaccard self-correlation, aggregated.

    Jaccard(WS_t, WS_{t-1}) = |∩| / |∪| for each tenant across consecutive
    steps. Returns the measured distribution.
    """
    vals: List[float] = []
    # Reindex per-tenant sequences (steps are already ordered).
    n = trace.n_steps
    prev: Dict[int, set] = {}
    for t in range(n):
        ws = trace.steps[t]
        for tid, cids in ws.items():
            cur = set(cids)
            if tid in prev:
                union = prev[tid] | cur
                if union:
                    vals.append(len(prev[tid] & cur) / len(union))
            prev[tid] = cur
    if not vals:
        return {"mean": 0.0, "p5": 0.0, "p50": 0.0, "p95": 0.0, "n": 0}
    arr = np.array(vals)
    return {
        "mean": float(arr.mean()),
        "p5": float(np.percentile(arr, 5)),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "n": int(arr.size),
    }


def measure_cfo_dedup(trace: Trace, cam_entries: int = 16) -> Dict[str, float]:
    """Fraction of cross-tenant requests coalesced by the real CFO CAM.

    Drives simcxl_ext.multi_tenant.CFOCoalesceModel with the trace's ACTUAL
    per-step cross-tenant chunk requests. Two tenants requesting the same
    physical chunk in the same step share one source read (same HMAC tag ->
    coalesced). The returned dedup fraction is a genuine dependent variable of
    the trace's overlap — feed a low-overlap trace and it drops, feed a
    high-overlap trace and it rises. It is NOT a constant.

    dedup_frac = coalesced_requests / total_requests
    """
    from simcxl_ext.multi_tenant import CFOCoalesceModel

    cfo = CFOCoalesceModel(cam_entries=cam_entries)
    total = 0
    saved = 0
    gated_steps = 0
    for ws in trace.steps:
        cfo.step_begin()
        # Interleave tenants so co-requested chunks land in the same CAM window
        # (a chunk is coalescable only while its entry is resident).
        tids = sorted(ws.keys())
        # Build a round-robin request order across tenants for this step.
        cols = [list(ws[t]) for t in tids]
        maxlen = max((len(c) for c in cols), default=0)
        for col_i in range(maxlen):
            for ti, t in enumerate(tids):
                if col_i < len(cols[ti]):
                    cid = cols[ti][col_i]
                    # Same physical chunk -> same source -> same tag (shared read).
                    tag = (hash(("src", cid)) & 0xFFFF_FFFF_FFFF_FFFF)
                    dma_needed, coalesced = cfo.request(str(t), cid, tag)
                    total += 1
                    if coalesced:
                        saved += 1
        cfo.step_end()
        if cfo.cam_gated:
            gated_steps += 1
    frac = saved / max(1, total)
    return {
        "dedup_frac": float(frac),
        "total_requests": int(total),
        "coalesced": int(saved),
        "gated_steps": int(gated_steps),
        "n_steps": trace.n_steps,
    }


# --------------------------------------------------------------------------- #
# Mode C staleness (RPE residual) — MEASURED from trace mechanics             #
# --------------------------------------------------------------------------- #
# This is the honest replacement for the hard-coded passive_evict_race_frac /
# passive_epoch_roll_frac constants in cxl_admission_sim.py. On a passive Type-3
# device the host runtime decides admission, then copies. Between decide and
# copy, an admit can go stale two ways:
#
#   1. eviction race : another host's admission this step targets a buffer slot
#      holding one of our pending chunks and evicts it before our copy lands.
#      We model the shared device buffer as a finite set of `buffer_slots`
#      chunk slots with the trace's actual admitted chunks competing for them;
#      the staleness count falls straight out of the collision statistics, so
#      it MOVES with the trace's real churn/overlap. No target constant.
#
#   2. epoch rollover : the endpoint bumps an epoch/nonce every `epoch_period`
#      steps; an admit whose copy straddles the bump is invalidated. This is
#      protocol-cadence driven and hence workload-independent by construction.
#
# The endpoint-gated modes (A/B) bind the verdict at the endpoint before the
# payload path, so neither race can occur -> measured RPE is structurally 0.
# We return the Mode-C residual as a fraction of fetched bytes, computed from
# the trace; whatever it is, it is.
def measure_modec_rpe(
    trace: Trace,
    n_hosts: int,
    buffer_bytes: int = 128 * 1024 * 1024,   # shared device staging buffer
    chunk_bytes: int = 64 * 1024,
    epoch_period: int = 64,
    copy_window_frac: float = 0.05,          # decide->copy-complete window as a
                                             # fraction of one decode step
    seed: int = 0,
) -> Dict[str, float]:
    """Replay the trace through a passive-Type-3 decide->copy race and COUNT
    stale bytes. Returns residual RPE (% of fetched) split into the two
    mechanisms. With n_hosts==1 there is no cross-host eviction race, so the
    eviction component is exactly 0 (single-host Mode C is safe).

    Physical model (all quantities measured from the trace, no target constant):

      * Vulnerability window: an admit is exposed from the SW decision until the
        host-driven copy completes. That window is `copy_window_frac` of a decode
        step. Other hosts' writes that land in the SAME shared buffer during that
        window can evict our pending chunk. Only writes within the window
        compete — this is the key physical scale, and it is swept in exp. D.

      * Eviction probability for one pending chunk occupying one of `slots`
        buffer slots, given `w` competing writes uniformly hitting the buffer
        during the window, is the exact occupancy probability
            p_evict = 1 - (1 - 1/slots)^w
        (NOT the linear w/slots, which overshoots and saturates spuriously).

      * Epoch rollover: an admit whose copy window straddles an epoch/nonce bump
        (every `epoch_period` steps) is invalidated. Protocol-cadence driven, so
        it is workload-independent by construction.

    Competing demand from the other (h-1) hosts is taken from the trace's own
    working-set density at rotated step indices, so eviction pressure scales
    with the trace's real churn — feed a low-overlap / large-buffer trace and it
    falls; feed a high-churn one and it rises. Whatever it yields, it yields.
    """
    rng = np.random.default_rng(seed)
    h = max(1, int(n_hosts))
    slots = max(1, buffer_bytes // chunk_bytes)

    fetched = 0          # total chunks a host commits to copy (== admitted)
    evict_stale = 0      # admits lost to a cross-host eviction race
    epoch_stale = 0      # admits lost to an epoch rollover mid-copy

    for t, ws in enumerate(trace.steps):
        # Our host's admitted chunks this step = union of tenant working sets.
        our_admits: List[int] = []
        for cids in ws.values():
            our_admits.extend(cids)
        if not our_admits:
            continue
        fetched += len(our_admits)

        # --- Epoch rollover: admits whose copy window straddles an epoch bump.
        # The window is a fraction of a step, so a straddle occurs only for the
        # tail `copy_window_frac` of steps just before a boundary.
        phase = t % epoch_period
        straddles_epoch = (epoch_period - 1 - phase) < copy_window_frac
        n_epoch_lost = 0
        if straddles_epoch:
            n_epoch_lost = len(our_admits)
            epoch_stale += n_epoch_lost

        # --- Eviction race: other hosts overwrite our pending slot pre-copy ---
        if h > 1:
            # Competing writes during OUR copy window. A slot is evicted only
            # when another host brings in a chunk that is NOT already resident,
            # i.e. a DISTINCT new chunk needs a slot. So buffer pressure is
            # driven by the number of *distinct* chunks the other hosts demand,
            # not their raw request volume. This is where the trace's overlap
            # enters: a high-overlap trace has its tenants (and thus the mirrored
            # other hosts) request the SAME hot chunks, so distinct pressure —
            # and eviction RPE — FALLS; a low-overlap trace raises it. The
            # dependency is measured, not assumed.
            distinct: set = set()
            for oh in range(1, h):
                rot = (t + oh * max(1, trace.n_steps // h)) % trace.n_steps
                for cids in trace.steps[rot].values():
                    distinct.update(cids)
            w = len(distinct) * copy_window_frac
            # Exact single-slot occupancy probability under w random writes.
            p_evict = 1.0 - (1.0 - 1.0 / slots) ** w
            # Only chunks that did NOT already die to epoch rollover are at risk.
            survivors = max(0, len(our_admits) - n_epoch_lost)
            if survivors > 0 and p_evict > 0:
                evict_stale += int(rng.binomial(survivors, p_evict))

    stale = evict_stale + epoch_stale
    denom = max(1, fetched)
    return {
        "n_hosts": h,
        "buffer_bytes": int(buffer_bytes),
        "copy_window_frac": float(copy_window_frac),
        "fetched_chunks": int(fetched),
        "evict_race_pct": 100.0 * evict_stale / denom,
        "epoch_roll_pct": 100.0 * epoch_stale / denom,
        "residual_rpe_pct": 100.0 * stale / denom,
    }
