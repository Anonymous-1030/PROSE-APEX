#!/usr/bin/env python3
"""Generate a per-event RPE log from the honest snapshot-vs-issue binding model.

This is a thin event-logger wrapper around the logic in trace_adapter/rpe_binding_model.py.
It emits one JSON line per descriptor issue with enough fields for burst-concentration
analysis (timestamp, descriptor id, trace, capacity, stale flag, instantaneous concurrency).

Time is measured in pool-admit ticks (admit_clock), which is the natural clock of the
binding model; downstream scripts treat these ticks as monotonic time units.
"""
from __future__ import annotations

import argparse
import csv
import heapq
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "trace_adapter"))

from rpe_binding_model import _Pool


def generate_event_log(
    trace_path: str,
    trace_name: str,
    buf_capacity: int,
    policy: str,
    n_tenants: int,
    queue_delay: int,
    max_events: int = 20000,
    seed: int = 42,
) -> list[dict]:
    rng = random.Random(seed)
    pool = _Pool(buf_capacity, policy)
    inflight: list[tuple[float, int, int, str, int, int]] = []
    seq = 0
    admit_clock = 0
    mean_res = max(1.0, queue_delay * (n_tenants / 8.0))
    events = []

    def drain(now):
        while inflight and inflight[0][0] <= now:
            issue_at, sid, frame, ck, gen, enq = heapq.heappop(inflight)
            stale = not pool.binding_valid(frame, ck, gen)
            # instantaneous concurrency at issue time: descriptors enqueued but not yet issued
            concurrent = sum(1 for ev in inflight if ev[5] <= now < ev[0]) + 1  # include self
            # pool occupancy at the moment the descriptor issues
            occupancy = pool.occupancy
            events.append({
                "event": "descriptor_issued",
                "descriptor_id": sid,
                "trace": trace_name,
                "capacity": buf_capacity,
                "enqueue_tick": enq,
                "issue_tick": int(issue_at),
                "timestamp_tick": int(issue_at),
                "stale": stale,
                "concurrent": concurrent,
                "occupancy": occupancy,
            })

    with open(trace_path, "r") as f:
        for row in csv.DictReader(f):
            if len(events) >= max_events:
                break
            sid = row["session_id"]
            try:
                nk = min(int(row["kv_chunks"]), 32)
            except (KeyError, ValueError):
                continue
            for c in range(nk):
                chunk_key = f"{sid}_{c}"
                frame, gen, was_res = pool.access(chunk_key)
                if not was_res:
                    admit_clock += 1
                residence = rng.expovariate(1.0 / mean_res)
                issue_at = admit_clock + residence
                enq = admit_clock
                heapq.heappush(inflight, (issue_at, seq, frame, chunk_key, gen, enq))
                seq += 1
                drain(admit_clock)

    drain(float("inf"))
    return events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=str, required=True, help="path to trace CSV")
    parser.add_argument("--trace-name", type=str, required=True)
    parser.add_argument("--capacity", type=int, required=True)
    parser.add_argument("--policy", type=str, default="LRU")
    parser.add_argument("--tenants", type=int, default=16)
    parser.add_argument("--queue-delay", type=int, default=64)
    parser.add_argument("--max-events", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    events = generate_event_log(
        args.trace, args.trace_name, args.capacity, args.policy,
        args.tenants, args.queue_delay, args.max_events, args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for ev in events:
            handle.write(json.dumps(ev) + "\n")
    print(f"Wrote {len(events)} events to {args.output}")


if __name__ == "__main__":
    main()
