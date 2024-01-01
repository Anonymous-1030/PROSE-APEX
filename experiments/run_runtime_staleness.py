#!/usr/bin/env python3
"""Measure descriptor staleness with real multi-process runtime timestamps.

This experiment closes the gap between an arrival-only trace and the actual
decide-to-dequeue race.  Host processes build 64-byte descriptors from the
repository's KV trace, a concurrent allocator process performs real residency
transitions, and the endpoint process timestamps queue dequeue.  Staleness is
counted only when the same chunk incarnation is evicted after descriptor
generation and before dequeue.

The long generation-to-dequeue interval is the stale-formation window.  It is
not the hardware atomic window.  Host cancellation ends at BDB/doorbell
submission; final endpoint validation linearizes at dequeue, immediately before
the separately reported dequeue-to-DMA commit interval (250 ns by default).

The resulting event stream is then replayed through a saturated single-server
SimCXL link model.  The replay preserves measured stale verdicts and changes
only the bytes serviced for a stale descriptor: passive software transfers the
64 KiB payload, while endpoint validation consumes metadata only.

Outputs:
  experiments/out/runtime_staleness/runtime_staleness.json
  experiments/out/runtime_staleness/runtime_trace_16p.jsonl
  experiments/out/runtime_staleness/fig_runtime_staleness.pdf
  experiments/out/runtime_staleness/fig_runtime_staleness.png
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import queue
import struct
import sys
import threading
import time
import zlib
from collections import Counter, deque
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from simcxl_ext.cxl_admission_sim import CHUNK_PAYLOAD_B, META_B

TRACE_PATH = ROOT / "experiments" / "out" / "data" / "trace.jsonl"
OUT_DIR = ROOT / "experiments" / "out" / "runtime_staleness"
HOST_COUNTS = [1, 2, 4, 8, 16, 32]
BW_GBS = [2.0, 4.0, 8.0]
COMPLETION_B = 64
ADMIT_VALIDATION_NS = 9.0
REJECT_VALIDATION_NS = 4.0


def _busy_wait_ns(duration_ns: int) -> None:
    duration_ns = max(0, duration_ns)
    deadline = time.perf_counter_ns() + duration_ns
    if duration_ns > 250_000:
        time.sleep((duration_ns - 100_000) / 1e9)
    while time.perf_counter_ns() < deadline:
        pass


def load_workloads(path: Path, max_steps: int = 80) -> tuple[dict[int, list[dict]], int]:
    """Load per-tenant descriptor records without materializing the full trace."""
    tenants: dict[int, list[dict]] = {}
    max_chunk = -1
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            rec = json.loads(line)
            if int(rec["step"]) >= max_steps:
                break
            tid = int(rec["tenant_id"])
            rows = tenants.setdefault(tid, [])
            for desc in rec.get("descriptors", []):
                cid = int(desc["chunk_id"])
                rows.append({
                    "step": int(rec["step"]),
                    "chunk_id": cid,
                    "priority": int(desc.get("priority", 0)),
                    "epoch": int(desc.get("epoch", 0)),
                })
                max_chunk = max(max_chunk, cid)
    if not tenants or max_chunk < 0:
        raise ValueError(f"no descriptors found in {path}")
    return tenants, max_chunk + 1


def _stable_snapshot(resident, epochs, chunk_id: int) -> tuple[bool, int]:
    """Read residency and incarnation without accepting a torn transition."""
    while True:
        before = int(epochs[chunk_id])
        is_resident = bool(resident[chunk_id])
        after = int(epochs[chunk_id])
        if before == after:
            return is_resident, after


def _producer(
    host_id: int,
    workload: list[dict],
    descriptor_queue,
    resident,
    epochs,
    start_event,
    ready_queue,
    origin_ns: int,
    interarrival_ns: int,
) -> None:
    ready_queue.put(("producer", host_id))
    start_event.wait()
    for index, item in enumerate(workload):
        gen_start = time.perf_counter_ns() - origin_ns
        # Match the hot path: pack a fixed-width descriptor and compute the
        # integrity word that would accompany the BDB entry.
        packed = struct.pack(
            "<HHBBHQQII", item["chunk_id"], item["epoch"],
            item["priority"], 0, host_id, index, item["step"],
            CHUNK_PAYLOAD_B, 0,
        )
        checksum = zlib.crc32(packed)
        snap_resident, snap_epoch = _stable_snapshot(
            resident, epochs, item["chunk_id"])
        gen_end = time.perf_counter_ns() - origin_ns
        desc_id = (host_id << 32) | index
        enqueue_ns = time.perf_counter_ns() - origin_ns
        descriptor_queue.put((
            "descriptor", desc_id, host_id, item["step"], item["chunk_id"],
            item["priority"], checksum, gen_start, gen_end, enqueue_ns,
            snap_resident, snap_epoch,
        ))
        _busy_wait_ns(interarrival_ns)
    descriptor_queue.put(("done", host_id))


def _allocator(
    capacity: int,
    initial_chunks: list[int],
    churn: list[int],
    resident,
    epochs,
    last_evict_ns,
    event_queue,
    start_event,
    stop_event,
    ready_queue,
    origin_ns: int,
    transition_interval_ns: int,
) -> None:
    lru = deque(initial_chunks)
    resident_set = set(initial_chunks)
    ready_queue.put(("allocator", 0))
    start_event.wait()
    next_transition = time.perf_counter_ns()
    index = 0
    while not stop_event.is_set():
        next_transition += transition_interval_ns
        remaining = next_transition - time.perf_counter_ns()
        if remaining > 250_000:
            time.sleep((remaining - 100_000) / 1e9)
        while time.perf_counter_ns() < next_transition:
            if stop_event.is_set():
                return
        chunk_id = churn[index % len(churn)]
        index += 1
        now = time.perf_counter_ns() - origin_ns
        if chunk_id in resident_set:
            try:
                lru.remove(chunk_id)
            except ValueError:
                pass
            lru.append(chunk_id)
            continue

        if len(resident_set) >= capacity:
            victim = lru.popleft()
            resident_set.remove(victim)
            resident[victim] = 0
            epochs[victim] = int(epochs[victim]) + 1
            last_evict_ns[victim] = now
            event_queue.put({
                "event": "residency_transition", "timestamp_ns": now,
                "chunk_id": victim, "state": "evicted",
                "incarnation": int(epochs[victim]),
            })

        resident_set.add(chunk_id)
        lru.append(chunk_id)
        resident[chunk_id] = 1
        event_queue.put({
            "event": "residency_transition", "timestamp_ns": now,
            "chunk_id": chunk_id, "state": "resident",
            "incarnation": int(epochs[chunk_id]),
        })


def classify_stale(
    snapshot_resident: bool,
    snapshot_epoch: int,
    current_epoch: int,
    last_evict: int,
    generation_end: int,
    dequeue_ns: int,
) -> bool:
    return bool(
        snapshot_resident
        and current_epoch != snapshot_epoch
        and generation_end < last_evict <= dequeue_ns
    )


def run_once(
    tenant_workloads: dict[int, list[dict]],
    n_chunks: int,
    n_hosts: int,
    descriptors_per_host: int,
    seed: int,
    service_ns: int,
    interarrival_ns: int,
    transition_interval_ns: int,
    capacity: int,
    atomic_commit_ns: int,
) -> dict[str, Any]:
    ctx = mp.get_context("spawn")
    rng = np.random.default_rng(seed)
    tids = sorted(tenant_workloads)
    workloads: list[list[dict]] = []
    for host_id in range(n_hosts):
        source = list(tenant_workloads[tids[host_id % len(tids)]])
        # Keep step ordering but rotate the start so repeats are independent.
        offset = int(rng.integers(0, max(1, len(source) - descriptors_per_host + 1)))
        workloads.append(source[offset: offset + descriptors_per_host])

    all_requested = [d["chunk_id"] for work in workloads for d in work]
    global_frequency = Counter(
        d["chunk_id"] for rows in tenant_workloads.values() for d in rows)
    initial_chunks = [cid for cid, _ in global_frequency.most_common(capacity)]
    if len(initial_chunks) < capacity:
        initial_chunks.extend(
            cid for cid in range(n_chunks) if cid not in set(initial_chunks)
        )
        initial_chunks = initial_chunks[:capacity]

    # Churn is trace-derived but biased toward chunks outside the initial hot
    # set, forcing the allocator to make real replacement decisions.
    cold = [cid for cid, _ in reversed(global_frequency.most_common())
            if cid not in set(initial_chunks)]
    hot = list(dict.fromkeys(all_requested))
    churn = cold + hot
    if not churn:
        churn = list(range(n_chunks))
    rng.shuffle(churn)

    resident = ctx.Array("b", n_chunks, lock=False)
    epochs = ctx.Array("I", n_chunks, lock=False)
    last_evict_ns = ctx.Array("Q", n_chunks, lock=False)
    for cid in initial_chunks:
        resident[cid] = 1

    descriptor_queue = ctx.Queue(maxsize=max(1024, n_hosts * descriptors_per_host))
    event_queue = ctx.Queue()
    ready_queue = ctx.Queue()
    start_event = ctx.Event()
    stop_event = ctx.Event()
    origin_ns = time.perf_counter_ns()

    allocator = ctx.Process(
        target=_allocator,
        args=(capacity, initial_chunks, churn, resident, epochs,
              last_evict_ns, event_queue, start_event, stop_event,
              ready_queue, origin_ns,
              transition_interval_ns * max(1, 8 // n_hosts)),
    )
    producers = [
        ctx.Process(
            target=_producer,
            args=(host_id, workloads[host_id], descriptor_queue, resident,
                  epochs, start_event, ready_queue, origin_ns,
                  interarrival_ns),
        )
        for host_id in range(n_hosts)
    ]
    allocator.start()
    for process in producers:
        process.start()
    for _ in range(n_hosts + 1):
        ready_queue.get(timeout=20)
    start_event.set()

    # A dedicated ingress thread drains the host/fabric transport into an
    # endpoint-owned queue.  This separates host-controllable transit from the
    # endpoint queue instead of conflating both in one generation-to-dequeue
    # interval.
    endpoint_queue: queue.Queue = queue.Queue()

    def ingress_worker() -> None:
        done_count = 0
        while done_count < n_hosts:
            message = descriptor_queue.get()
            if message[0] == "done":
                done_count += 1
                continue
            endpoint_queue.put((message, time.perf_counter_ns() - origin_ns))
        endpoint_queue.put((None, None))

    ingress_thread = threading.Thread(target=ingress_worker, daemon=True)
    ingress_thread.start()

    rows: list[dict[str, Any]] = []
    while True:
        try:
            message, ingress_ns = endpoint_queue.get(timeout=10)
        except queue.Empty as exc:
            raise RuntimeError("endpoint ingress stalled") from exc
        if message is None:
            break
        (
            _, desc_id, host_id, step, chunk_id, priority, checksum,
            gen_start, gen_end, enqueue_ns, snap_resident, snap_epoch,
        ) = message
        _busy_wait_ns(service_ns)
        dequeue_ns = time.perf_counter_ns() - origin_ns
        current_epoch = int(epochs[chunk_id])
        evict_ns = int(last_evict_ns[chunk_id])
        stale = classify_stale(
            snap_resident, snap_epoch, current_epoch, evict_ns,
            gen_end, dequeue_ns,
        )
        if stale:
            if evict_ns <= enqueue_ns:
                eviction_stage = "host_preparation"
            elif evict_ns <= ingress_ns:
                eviction_stage = "submitted_host_or_fabric"
            else:
                eviction_stage = "endpoint_queue"
        else:
            eviction_stage = None
        rows.append({
            "event": "descriptor_dequeued", "descriptor_id": int(desc_id),
            "host_id": int(host_id), "step": int(step),
            "chunk_id": int(chunk_id), "priority": int(priority),
            "checksum": int(checksum), "generation_start_ns": int(gen_start),
            "generation_end_ns": int(gen_end),
            "generation_time_ns": int(gen_end - gen_start),
            "enqueue_ns": int(enqueue_ns),
            "endpoint_ingress_ns": int(ingress_ns),
            "dequeue_ns": int(dequeue_ns),
            "validation_linearization_ns": int(dequeue_ns),
            "commit_ns": int(dequeue_ns + atomic_commit_ns),
            "atomic_commit_window_ns": int(atomic_commit_ns),
            "window_ns": int(dequeue_ns - gen_end),
            "host_control_ns": int(ingress_ns - gen_start),
            "endpoint_queue_ns": int(dequeue_ns - ingress_ns),
            "resident_at_generation": bool(snap_resident),
            "incarnation_at_generation": int(snap_epoch),
            "incarnation_at_dequeue": current_epoch,
            "eviction_ns": evict_ns if stale else None,
            "eviction_stage": eviction_stage,
            "host_could_cancel_at_eviction": bool(
                stale and evict_ns <= enqueue_ns),
            "stale": stale,
        })

    ingress_thread.join(timeout=10)

    for process in producers:
        process.join(timeout=10)
        if process.exitcode != 0:
            raise RuntimeError(f"producer exited with code {process.exitcode}")
    stop_event.set()
    allocator.join(timeout=10)
    if allocator.is_alive():
        allocator.terminate()
        allocator.join()

    transitions = []
    while True:
        try:
            transitions.append(event_queue.get_nowait())
        except queue.Empty:
            break

    windows = np.array([r["window_ns"] for r in rows], dtype=float)
    gen_times = np.array([r["generation_time_ns"] for r in rows], dtype=float)
    stale_mask = np.array([r["stale"] for r in rows], dtype=bool)
    eligible = np.array([r["resident_at_generation"] for r in rows], dtype=bool)
    stage_counts = Counter(
        r["eviction_stage"] for r in rows if r["eviction_stage"] is not None)
    post_evict = np.array([
        r["dequeue_ns"] - r["eviction_ns"] for r in rows if r["stale"]
    ], dtype=float)
    time_to_evict = np.array([
        r["eviction_ns"] - r["generation_end_ns"] for r in rows if r["stale"]
    ], dtype=float)
    run_start_ns = min(r["generation_start_ns"] for r in rows)
    run_end_ns = max(r["dequeue_ns"] for r in rows)
    run_duration_s = max(1e-12, (run_end_ns - run_start_ns) / 1e9)
    generation_span_s = max(
        1e-12,
        (max(r["generation_end_ns"] for r in rows) -
         min(r["generation_end_ns"] for r in rows)) / 1e9,
    )
    exposure_s = float(windows.sum() / 1e9)
    eviction_transitions = sum(
        event.get("state") == "evicted" for event in transitions)
    return {
        "n_hosts": n_hosts,
        "seed": seed,
        "rows": rows,
        "transitions": transitions,
        "summary": {
            "descriptors": len(rows),
            "eligible_resident": int(eligible.sum()),
            "stale_descriptors": int(stale_mask.sum()),
            "stale_all_pct": float(stale_mask.mean() * 100.0),
            "stale_eligible_pct": float(
                stale_mask.sum() / max(1, eligible.sum()) * 100.0),
            "generation_p50_us": float(np.percentile(gen_times, 50) / 1e3),
            "generation_p99_us": float(np.percentile(gen_times, 99) / 1e3),
            "window_p50_us": float(np.percentile(windows, 50) / 1e3),
            "window_p99_us": float(np.percentile(windows, 99) / 1e3),
            "stale_window_p50_us": float(
                np.percentile(windows[stale_mask], 50) / 1e3
                if stale_mask.any() else 0.0),
            "stale_window_p99_us": float(
                np.percentile(windows[stale_mask], 99) / 1e3
                if stale_mask.any() else 0.0),
            "time_to_eviction_p50_us": float(
                np.percentile(time_to_evict, 50) / 1e3
                if time_to_evict.size else 0.0),
            "time_to_eviction_p95_us": float(
                np.percentile(time_to_evict, 95) / 1e3
                if time_to_evict.size else 0.0),
            "post_eviction_residence_p50_us": float(
                np.percentile(post_evict, 50) / 1e3
                if post_evict.size else 0.0),
            "post_eviction_residence_p95_us": float(
                np.percentile(post_evict, 95) / 1e3
                if post_evict.size else 0.0),
            "post_eviction_residence_p99_us": float(
                np.percentile(post_evict, 99) / 1e3
                if post_evict.size else 0.0),
            "eviction_stage_counts": dict(stage_counts),
            "endpoint_owned_stale_pct": float(
                stage_counts.get("endpoint_queue", 0) /
                max(1, int(stale_mask.sum())) * 100.0),
            "post_cancellation_stale_pct": float(
                (stage_counts.get("submitted_host_or_fabric", 0) +
                 stage_counts.get("endpoint_queue", 0)) /
                max(1, int(stale_mask.sum())) * 100.0),
            "aggregate_descriptor_exposure_s": exposure_s,
            "eviction_hazard_per_descriptor_s": float(
                stale_mask.sum() / max(exposure_s, 1e-12)),
            "descriptor_arrival_rate_per_s": float(len(rows) / generation_span_s),
            "mean_inflight_descriptors": float(
                (len(rows) / generation_span_s) * windows.mean() / 1e9),
            "unique_target_chunks": len({r["chunk_id"] for r in rows}),
            "allocator_evictions_per_s": float(
                eviction_transitions / run_duration_s),
            "run_duration_s": run_duration_s,
            "residency_transitions": len(transitions),
        },
    }


def bootstrap_ci(values: Iterable[float], seed: int = 0) -> tuple[float, float, float]:
    vals = np.asarray(list(values), dtype=float)
    mean = float(vals.mean())
    if vals.size == 1:
        return mean, mean, mean
    rng = np.random.default_rng(seed)
    samples = rng.choice(vals, size=(10000, vals.size), replace=True).mean(axis=1)
    return mean, float(np.percentile(samples, 2.5)), float(np.percentile(samples, 97.5))


def replay_simcxl(rows: list[dict], bandwidth_gbs: Iterable[float]) -> dict[str, list[float]]:
    """Replay measured verdicts through saturated passive and gated links."""
    ordered = sorted(rows, key=lambda row: row["generation_end_ns"])
    if not ordered:
        return {"bandwidth_gbs": [], "passive_desc_per_s": [],
                "endpoint_desc_per_s": [], "speedup": []}
    stale = np.array([r["stale"] for r in ordered], dtype=bool)
    passive_rates, endpoint_rates = [], []
    passive_busy_ms, endpoint_busy_ms = [], []
    passive_bytes = float(len(ordered) * CHUNK_PAYLOAD_B)
    endpoint_bytes = float(
        (~stale).sum() * (CHUNK_PAYLOAD_B + META_B) + stale.sum() * META_B)
    for bw in bandwidth_gbs:
        bytes_per_s = float(bw) * 1e9
        passive_s = passive_bytes / bytes_per_s
        endpoint_s = endpoint_bytes / bytes_per_s
        passive_busy_ms.append(passive_s * 1e3)
        endpoint_busy_ms.append(endpoint_s * 1e3)
        passive_rates.append(len(ordered) / max(passive_s, 1e-12))
        endpoint_rates.append(len(ordered) / max(endpoint_s, 1e-12))
    return {
        "bandwidth_gbs": [float(v) for v in bandwidth_gbs],
        "passive_desc_per_s": passive_rates,
        "endpoint_desc_per_s": endpoint_rates,
        "speedup": [g / p for g, p in zip(endpoint_rates, passive_rates)],
        "passive_link_busy_ms": passive_busy_ms,
        "endpoint_link_busy_ms": endpoint_busy_ms,
        "stale_payload_mib": float(stale.sum() * CHUNK_PAYLOAD_B / 2**20),
        "payload_reduction_pct": float((passive_bytes - endpoint_bytes) /
                                       passive_bytes * 100.0),
    }


def replay_tail_latency(
    rows: list[dict],
    bandwidth_gbs: float = 4.0,
    offered_load: Iterable[float] = (0.60, 0.80, 0.90, 0.96, 1.00, 1.04, 1.08, 1.12),
) -> dict[str, Any]:
    """Replay a measured stale sequence at fixed offered loads.

    Offered load is normalized to the passive payload-only link capacity.  The
    finite FIFO replay exposes the nonlinear queueing effect that a byte-ratio
    calculation cannot: near saturation, removing stale payload can keep the
    endpoint-gated queue stable while the passive queue accumulates backlog.
    """
    ordered = sorted(rows, key=lambda row: row["generation_end_ns"])
    stale = np.array([row["stale"] for row in ordered], dtype=bool)
    loads = np.asarray(list(offered_load), dtype=float)
    bytes_per_s = float(bandwidth_gbs) * 1e9
    passive_capacity = bytes_per_s / CHUNK_PAYLOAD_B
    endpoint_service_s = np.where(
        stale,
        (META_B + COMPLETION_B) / bytes_per_s + REJECT_VALIDATION_NS / 1e9,
        (CHUNK_PAYLOAD_B + META_B + COMPLETION_B) / bytes_per_s +
        ADMIT_VALIDATION_NS / 1e9,
    )
    endpoint_capacity = 1.0 / float(endpoint_service_s.mean())

    output: dict[str, Any] = {
        "bandwidth_gbs": float(bandwidth_gbs),
        "offered_load": loads.tolist(),
        "passive_capacity_desc_per_s": float(passive_capacity),
        "endpoint_capacity_desc_per_s": float(endpoint_capacity),
        "endpoint_saturation_load_pct": float(
            endpoint_capacity / passive_capacity * 100.0),
        "cost_model": {
            "included": [
                f"{META_B}-byte descriptor control traffic",
                f"{COMPLETION_B}-byte completion traffic",
                f"{REJECT_VALIDATION_NS:g} ns reject validation",
                f"{ADMIT_VALIDATION_NS:g} ns admit validation",
                "FIFO link queueing on one shared total-byte bandwidth",
            ],
            "excluded": [
                "pin-table contention beyond synthesized validation cycles",
                "independent descriptor-rate bottlenecks",
                "CFO and endpoint scoring",
            ],
        },
    }
    for mode in ("passive", "endpoint"):
        p50_ms, p99_ms, peak_depth, achieved = [], [], [], []
        for load in loads:
            arrival_rate = float(load) * passive_capacity
            arrivals = np.arange(len(ordered), dtype=float) / arrival_rate
            finish = 0.0
            latencies = []
            in_system: deque[float] = deque()
            peak = 0
            for index, arrival in enumerate(arrivals):
                while in_system and in_system[0] <= arrival:
                    in_system.popleft()
                if mode == "passive":
                    service_s = CHUNK_PAYLOAD_B / bytes_per_s
                else:
                    service_s = float(endpoint_service_s[index])
                finish = max(finish, float(arrival)) + service_s
                in_system.append(finish)
                peak = max(peak, len(in_system))
                latencies.append(finish - float(arrival))
            latency = np.asarray(latencies)
            elapsed = max(finish - arrivals[0], 1e-12)
            p50_ms.append(float(np.percentile(latency, 50) * 1e3))
            p99_ms.append(float(np.percentile(latency, 99) * 1e3))
            peak_depth.append(int(peak))
            achieved.append(float(len(ordered) / elapsed))
        output[mode] = {
            "p50_ms": p50_ms,
            "p99_ms": p99_ms,
            "peak_queue_depth": peak_depth,
            "achieved_desc_per_s": achieved,
        }
    output["stale_pct"] = float(stale.mean() * 100.0)
    return output


def _ecdf(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.sort(values)
    return x, np.arange(1, len(x) + 1) / max(1, len(x))


def plot_figure(
    results: dict[str, Any],
    representative: dict[str, Any],
    tail_replay: dict[str, Any],
) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    # 1x4 double-column (figure*) layout: four panels in one horizontal row.
    # Each panel is ~1.65" wide x ~2.0" tall to avoid overly flat rectangles.
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 5.8,
        "axes.labelsize": 5.8, "axes.titlesize": 6.2,
        "xtick.labelsize": 5.2, "ytick.labelsize": 5.2,
        "legend.fontsize": 5.0, "axes.linewidth": 0.55,
        "lines.linewidth": 1.1, "figure.dpi": 240,
        "pdf.fonttype": 42, "ps.fonttype": 42,
    })
    navy, blue, red, green, gold, gray, black = (
        "#24557A", "#6BA7C9", "#C23B32", "#2A7F62",
        "#D99A2B", "#A8AFB5", "#202428",
    )

    fig = plt.figure(figsize=(7.16, 1.45))
    grid = fig.add_gridspec(
        1, 4, wspace=0.30,
        left=0.030, right=0.985, top=0.760, bottom=0.175,
    )
    axa = fig.add_subplot(grid[0, 0])
    axb = fig.add_subplot(grid[0, 1])
    axc = fig.add_subplot(grid[0, 2])
    axd = fig.add_subplot(grid[0, 3])

    rows = representative["rows"]
    # Minimal sample for the very flat timeline panel.
    sample_indices = np.linspace(0, len(rows) - 1, 3, dtype=int)
    sample = [rows[index] for index in sample_indices]
    for y, row in enumerate(sample):
        origin = row["generation_start_ns"]
        enqueue = (row["enqueue_ns"] - origin) / 1e3
        ingress = (row["endpoint_ingress_ns"] - origin) / 1e3
        dequeue_us = (row["dequeue_ns"] - origin) / 1e3
        commit_us = (row["commit_ns"] - origin) / 1e3
        axa.hlines(y, 0, enqueue, color=gray, lw=1.8)
        axa.hlines(y, enqueue, ingress, color=blue, lw=1.8)
        axa.hlines(y, ingress, dequeue_us, color=navy, lw=1.8)
        axa.hlines(y, dequeue_us, commit_us, color=black, lw=2.4)
        axa.plot(enqueue, y, marker="|", ms=5, mew=0.9, color=gold)
        axa.plot(dequeue_us, y, "o", ms=2.0, color=black)
        if row["stale"] and row["eviction_ns"] is not None:
            xe = (row["eviction_ns"] - origin) / 1e3
            axa.plot(xe, y, marker="x", ms=3.5, mew=0.9, color=red)
    axa.set_xlabel("time (\u00b5s)", labelpad=0)
    axa.set_ylabel("")
    axa.set_title("(a) Descriptor lifecycle", loc="left", fontweight="bold", pad=1)
    axa.grid(axis="x", color="#D7DBDE", lw=0.35, alpha=0.7)
    axa.set_ylim(-0.5, len(sample) + 0.2)
    axa.set_xticks([0, 250000, 500000])
    axa.set_xticklabels(["0", "250k", "500k"], fontsize=5.0)
    # Compact legend only for key items.
    legend_items = [
        Line2D([0], [0], color=blue, lw=1.8, label="host\u2192fabric"),
        Line2D([0], [0], color=navy, lw=1.8, label="endpoint q"),
        Line2D([0], [0], color=red, marker="x", lw=0, ms=4, label="evict"),
    ]
    axa.legend(handles=legend_items, frameon=True, loc="lower right",
               bbox_to_anchor=(0.99, 0.02), ncol=1, fontsize=4.4,
               columnspacing=0.18, handlelength=0.60,
               facecolor="white", edgecolor="#D7DBDE", framealpha=0.94)

    gen_us = np.array([r["generation_time_ns"] for r in rows]) / 1e3
    clean_us = np.array([r["window_ns"] for r in rows if not r["stale"]]) / 1e3
    stale_us = np.array([r["window_ns"] for r in rows if r["stale"]]) / 1e3
    time_to_evict_us = np.array([
        r["eviction_ns"] - r["generation_end_ns"] for r in rows if r["stale"]
    ]) / 1e3
    post_evict_us = np.array([
        r["dequeue_ns"] - r["eviction_ns"] for r in rows if r["stale"]
    ]) / 1e3
    for vals, color, label, ls, lw, alpha in (
        (gen_us, gold, "construct", "-", 0.9, 0.48),
        (clean_us, navy, "resident", "-", 0.9, 0.42),
        (stale_us, red, "stale", "--", 1.5, 1.0),
        (time_to_evict_us, green, "to-evict", "-.", 1.7, 1.0),
        (post_evict_us, black, "post-evict", ":", 1.7, 1.0),
    ):
        if vals.size:
            x, y = _ecdf(vals)
            axb.plot(x, y, color=color, ls=ls, lw=lw, alpha=alpha, label=label)
    axb.set_xscale("log")
    axb.set_xlim(left=max(0.1, min(gen_us.min(), clean_us.min()) * 0.7))
    axb.set_ylim(0, 1.02)
    axb.set_xlabel("latency (\u00b5s, log)", labelpad=0)
    axb.set_ylabel("")
    axb.set_title("(b) Latency CDFs", loc="left", fontweight="bold", pad=1)
    axb.grid(color="#D7DBDE", lw=0.35, alpha=0.7)
    axb.legend(frameon=True, framealpha=0.92, edgecolor="#D7DBDE",
               loc="center", bbox_to_anchor=(0.52, 0.52), ncol=2,
               fontsize=4.0, handlelength=0.80)
    axb.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    axb.set_yticklabels(["0.00", "0.25", "0.50", "0.75", ""], fontsize=5.0)

    hosts = results["host_counts"]
    means, lows, highs, p99s, hazards = [], [], [], [], []
    for h in hosts:
        runs = results["runs"][str(h)]
        mean, low_ci, high_ci = bootstrap_ci(
            [run["summary"]["stale_all_pct"] for run in runs], seed=h)
        means.append(mean); lows.append(low_ci); highs.append(high_ci)
        p99s.append(float(np.median([run["summary"]["window_p99_us"] for run in runs])))
        hazards.append(float(np.median([
            run["summary"]["eviction_hazard_per_descriptor_s"] for run in runs
        ])))
    axc.errorbar(hosts, means,
                 yerr=[np.array(means) - lows, np.array(highs) - means],
                 color=red, marker="o", capsize=2.6, ms=4.0,
                 label="stale %")
    axc.set_xscale("log", base=2)
    axc.set_xticks(hosts)
    axc.set_xticklabels([str(h) for h in hosts], fontsize=5.0)
    axc.set_ylabel("stale (%)", color=red, labelpad=0)
    axc.tick_params(axis="y", colors=red, labelsize=5.2)
    axc.grid(color="#D7DBDE", lw=0.35, alpha=0.7)
    axc.set_title("(c) Concurrency", loc="left", fontweight="bold", pad=1)
    axc.set_xlabel("# producers", labelpad=0)
    # P99 descriptor-to-dequeue window on secondary y-axis.
    axc2 = axc.twinx()
    p99_s = np.array(p99s) / 1e6 if p99s else np.array([])
    axc2.plot(hosts, p99_s, color=green, marker="D", ms=3.0,
              ls="--", lw=1.3, label="P99 window (s)")
    axc2.tick_params(axis="y", colors=green, labelsize=5.2)
    axc2.spines["right"].set_color(green)
    axc2.set_ylim(0, max(p99_s.max() * 1.15, 1e-9))
    # Omit the right y-axis label to avoid crowding the adjacent panel.
    axc2.set_ylabel("")
    lines1, labels1 = axc.get_legend_handles_labels()
    lines2, labels2 = axc2.get_legend_handles_labels()
    axc.legend(lines1 + lines2, labels1 + labels2,
               frameon=True, framealpha=0.92, edgecolor="#D7DBDE",
               loc="upper left", fontsize=4.0, handlelength=0.80)

    load_pct = np.array(tail_replay["offered_load"]) * 100.0
    passive_p99 = np.array(tail_replay["passive"]["p99_ms"])
    endpoint_p99 = np.array(tail_replay["endpoint"]["p99_ms"])
    passive_q = np.array(tail_replay["passive"]["peak_queue_depth"])
    endpoint_q = np.array(tail_replay["endpoint"]["peak_queue_depth"])

    axd.plot(load_pct, passive_p99, color=red, marker="D", ls="--", ms=3.0,
             label="passive")
    axd.plot(load_pct, endpoint_p99, color=green, marker="o", ms=3.0,
             label="endpoint")
    axd.set_yscale("log")
    axd.set_ylabel("P99 (ms)", color=red, labelpad=2)
    axd.tick_params(axis="y", colors=red, labelsize=5.8,
                    left=False, labelleft=False)
    axd.yaxis.tick_right()
    axd.yaxis.set_label_position("right")
    axd.spines["left"].set_visible(True)
    axd.spines["left"].set_color("#202428")
    axd.spines["right"].set_visible(True)
    axd.spines["right"].set_color(red)
    axd.set_title(
        f"(d) SimCXL replay @ {tail_replay['bandwidth_gbs']:g} GB/s",
        loc="left", fontweight="bold", pad=1,
    )
    axd.grid(color="#D7DBDE", lw=0.4, alpha=0.7)
    axd.axvline(100, color=gray, lw=0.7, ls=":")
    endpoint_sat = float(tail_replay["endpoint_saturation_load_pct"])
    axd.axvline(endpoint_sat, color=green, lw=0.7, ls=":")
    axd.axvspan(100, endpoint_sat, color=green, alpha=0.08, lw=0)

    axd.set_xlabel("load (%)", labelpad=0)
    axd.legend(frameon=True, framealpha=0.92, edgecolor="#D7DBDE",
               loc="upper left", fontsize=4.4, handlelength=0.80)
    axd.text(0.98, 0.95,
             f"sat. 100\u2192{endpoint_sat:.0f}%",
             transform=axd.transAxes, ha="right", va="top",
             fontsize=5.0, color=green)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf = OUT_DIR / "fig_runtime_staleness.pdf"
    fig.savefig(pdf, bbox_inches="tight", pad_inches=0.02)
    fig.savefig(pdf.with_suffix(".png"), bbox_inches="tight", dpi=240, pad_inches=0.02)
    plt.close(fig)
    return pdf


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, default=TRACE_PATH)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--descriptors-per-host", type=int, default=300)
    parser.add_argument("--service-us", type=float, default=120.0)
    parser.add_argument("--interarrival-us", type=float, default=600.0)
    parser.add_argument("--transition-us", type=float, default=500.0)
    parser.add_argument("--capacity", type=int, default=256)
    parser.add_argument("--atomic-commit-ns", type=int, default=250,
                        help="Endpoint dequeue-to-DMA linearization window.")
    parser.add_argument("--host-counts", type=int, nargs="+", default=HOST_COUNTS,
                        help="Promotion-producer counts to execute.")
    parser.add_argument("--resume", action="store_true",
                        help="Replace selected host counts in existing results.")
    parser.add_argument("--plot-only", action="store_true",
                        help="Regenerate the figure from the saved JSON/JSONL outputs.")
    args = parser.parse_args()

    if args.plot_only:
        with (OUT_DIR / "runtime_staleness.json").open("r", encoding="utf-8") as handle:
            results = json.load(handle)
        saved_events = [
            json.loads(line)
            for line in (OUT_DIR / "runtime_trace_16p.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()
        ]
        representative = {
            "rows": [e for e in saved_events if e.get("event") == "descriptor_dequeued"],
            "transitions": [e for e in saved_events
                            if e.get("event") == "residency_transition"],
        }
        tail_replay = replay_tail_latency(representative["rows"])
        results["simcxl_tail_replay"] = tail_replay
        with (OUT_DIR / "runtime_staleness.json").open("w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)
        figure = plot_figure(results, representative, tail_replay)
        print(f"Saved figure: {figure}")
        return 0

    tenant_workloads, n_chunks = load_workloads(args.trace)
    if args.capacity >= n_chunks:
        raise ValueError("capacity must be smaller than the trace chunk universe")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.resume and (OUT_DIR / "runtime_staleness.json").exists():
        with (OUT_DIR / "runtime_staleness.json").open("r", encoding="utf-8") as handle:
            previous = json.load(handle)
        compact_runs = dict(previous.get("runs", {}))
    else:
        compact_runs: dict[str, list[dict]] = {}
    representative = None
    for n_hosts in args.host_counts:
        compact_runs[str(n_hosts)] = []
        for repeat in range(args.repeats):
            run = run_once(
                tenant_workloads, n_chunks, n_hosts,
                args.descriptors_per_host, seed=1000 + 37 * repeat + n_hosts,
                service_ns=int(args.service_us * 1e3),
                interarrival_ns=int(args.interarrival_us * 1e3),
                transition_interval_ns=int(args.transition_us * 1e3),
                capacity=args.capacity,
                atomic_commit_ns=args.atomic_commit_ns,
            )
            compact_runs[str(n_hosts)].append({
                "seed": run["seed"], "summary": run["summary"]
            })
            s = run["summary"]
            print(
                f"{n_hosts:>2} producers rep {repeat + 1}/{args.repeats}: "
                f"stale={s['stale_all_pct']:.2f}% "
                f"window_p99={s['window_p99_us']:.1f} us "
                f"gen_p99={s['generation_p99_us']:.2f} us"
            )
            if n_hosts == 16 and repeat == 0:
                representative = run

    if representative is None:
        saved_events = [
            json.loads(line)
            for line in (OUT_DIR / "runtime_trace_16p.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()
        ]
        representative = {
            "rows": [e for e in saved_events if e.get("event") == "descriptor_dequeued"],
            "transitions": [e for e in saved_events
                            if e.get("event") == "residency_transition"],
            "summary": previous["representative_16p_summary"],
        }
    replay = replay_simcxl(representative["rows"], BW_GBS)
    tail_replay = replay_tail_latency(representative["rows"])
    results = {
        "provenance": {
            "trace": str(args.trace.resolve()),
            "trace_kind": "repository KV descriptor trace",
            "runtime": "real OS multi-process queue and allocator timestamps",
            "hardware": "process-emulated hosts; no claim of physical CXL hosts",
            "clock": "time.perf_counter_ns (monotonic)",
        },
        "config": {
            "repeats": args.repeats,
            "descriptors_per_host": args.descriptors_per_host,
            "service_us": args.service_us,
            "interarrival_us": args.interarrival_us,
            "transition_us": args.transition_us,
            "allocator_capacity_chunks": args.capacity,
            "atomic_commit_ns": args.atomic_commit_ns,
            "chunk_payload_bytes": CHUNK_PAYLOAD_B,
            "metadata_bytes": META_B,
            "bootstrap_unit": "independent run",
            "queue_ownership": {
                "host_control_ends": "enqueue_ns (BDB/doorbell submission)",
                "endpoint_owned_queue": "endpoint_ingress_ns to dequeue_ns",
                "validation_linearization": "dequeue_ns",
                "dma_commit": "dequeue_ns + atomic_commit_ns (hardware parameter)",
            },
        },
        "host_counts": HOST_COUNTS,
        "runs": compact_runs,
        "simcxl_replay": replay,
        "simcxl_tail_replay": tail_replay,
        "representative_16p_summary": representative["summary"],
    }
    with (OUT_DIR / "runtime_staleness.json").open("w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)

    events = representative["transitions"] + representative["rows"]
    events.sort(key=lambda event: event.get("timestamp_ns", event.get("dequeue_ns", 0)))
    with (OUT_DIR / "runtime_trace_16p.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    figure = plot_figure(results, representative, tail_replay)
    print(f"Saved data:   {OUT_DIR / 'runtime_staleness.json'}")
    print(f"Saved trace:  {OUT_DIR / 'runtime_trace_16p.jsonl'}")
    print(f"Saved figure: {figure}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
