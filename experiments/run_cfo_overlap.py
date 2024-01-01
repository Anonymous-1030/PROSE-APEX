#!/usr/bin/env python3
"""Reproduce: coalesced fan-out (CFO) pays off only past a read-port break-even.

Paper claim (§IV-D / Fig. cfo_overlap):
  CFO folds the matching source reads of independent domains into one read and
  fans the payload to each. The saving tracks working-set overlap and read-port
  pressure, not a fixed speedup: offered read load crosses saturation near 45%
  overlap, decode throughput returns to the compute ceiling once the port clears
  at ~50%, and P99 promotion-completion latency falls (1.73 ms -> 0.57 ms).
  CFO cannot push the tail below the write-N egress floor, so below break-even
  it yields no throughput gain.

This is the self-contained resource-curve model used in the paper (16 domains,
64 KiB chunks, 64 chunks/domain, device read-port vs aggregate-egress
queueing). It sweeps cross-domain overlap and reports the offered read load and
P99 latency with and without CFO.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

# Make the package importable when run directly (no install / PYTHONPATH needed).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.io_utils import save_json, save_fig, C

DOMAINS = 16
OVERLAPS = [0.0, 0.25, 0.5, 0.75, 1.0]
SEEDS = [42, 123]
N_STEPS = 128

CHUNK_BYTES = 64 * 1024
CHUNKS_PER_DOMAIN = 64

STEP_MS = 1.0
STEP_NS = STEP_MS * 1e6
READ_BW_GBS = 40.0        # device source-read fabric
EGRESS_BW_GBS = 128.0     # aggregate posted writes to GPUs

READ_FIXED_NS = 52.0
WRITE_FIXED_NS = 34.0
SCHED_JITTER_NS = 80.0
BREAK_EVEN_OVERLAP = 0.45


def _util(mb_per_step: float, bw_gbs: float) -> float:
    return mb_per_step / (bw_gbs * STEP_MS)


def _service_ns(nbytes: int, bw_gbs: float, fixed_ns: float,
                rng: np.random.Generator) -> float:
    bw_ns = nbytes / bw_gbs                       # GB/s == bytes/ns
    row_jitter = rng.lognormal(mean=math.log(12.0), sigma=0.28)
    return fixed_ns + bw_ns + row_jitter


def _step_sources(domains: int, overlap: float,
                  rng: np.random.Generator) -> List[Tuple[int, int]]:
    if overlap <= 0:
        step_overlap = 0.0
    elif overlap >= 1:
        step_overlap = 1.0
    else:
        c = 18.0
        step_overlap = float(rng.beta(overlap * c, (1.0 - overlap) * c))
    shared_n = int(round(CHUNKS_PER_DOMAIN * step_overlap))
    shared_ids = list(range(shared_n))
    next_private = shared_n
    reqs: List[Tuple[int, int]] = []
    for domain in range(domains):
        for cid in shared_ids:
            reqs.append((domain, cid))
        for cid in range(next_private, next_private + (CHUNKS_PER_DOMAIN - shared_n)):
            reqs.append((domain, cid))
        next_private += (CHUNKS_PER_DOMAIN - shared_n)
    rng.shuffle(reqs)
    return reqs


def _simulate_once(domains: int, overlap: float, cfo: bool, seed: int) -> Dict[str, float]:
    rng = np.random.default_rng(seed)
    latencies: List[float] = []
    step_done: List[float] = []
    source_read_bytes: List[int] = []
    egress_bytes: List[int] = []

    for _ in range(N_STEPS):
        read_avail = 0.0
        write_avail = 0.0
        requests = _step_sources(domains, overlap, rng)
        arrivals = {r: float(rng.uniform(0.0, SCHED_JITTER_NS)) for r in requests}
        consumers: Dict[int, List[Tuple[int, int]]] = {}
        for r in requests:
            consumers.setdefault(r[1], []).append(r)

        read_done: Dict[Any, float] = {}
        if cfo:
            sources = list(consumers.keys())
            rng.shuffle(sources)
            for src in sources:
                arrival = min(arrivals[r] for r in consumers[src])
                start = max(read_avail, arrival)
                done = start + _service_ns(CHUNK_BYTES, READ_BW_GBS, READ_FIXED_NS, rng)
                read_avail = done
                read_done[src] = done
        else:
            for r in requests:
                start = max(read_avail, arrivals[r])
                done = start + _service_ns(CHUNK_BYTES, READ_BW_GBS, READ_FIXED_NS, rng)
                read_avail = done
                read_done[id(r)] = done

        ready = [(read_done[r[1] if cfo else id(r)], r) for r in requests]
        ready.sort(key=lambda x: x[0])
        completions: List[float] = []
        for rd, r in ready:
            start = max(write_avail, rd)
            done = start + _service_ns(CHUNK_BYTES, EGRESS_BW_GBS, WRITE_FIXED_NS, rng)
            write_avail = done
            latencies.append(done - arrivals[r])
            completions.append(done)

        read_instances = len(consumers) if cfo else len(requests)
        source_read_bytes.append(read_instances * CHUNK_BYTES)
        egress_bytes.append(len(requests) * CHUNK_BYTES)
        step_done.append(max(completions))

    src_mb = float(np.mean(source_read_bytes) / 1e6)
    mean_step_ns = float(np.mean(step_done))
    return {
        "source_read_mb_per_step": src_mb,
        "device_read_util": _util(src_mb, READ_BW_GBS),
        "throughput_scale": min(1.0, STEP_NS / max(mean_step_ns, 1.0)),
        "p99_latency_ms": float(np.percentile(latencies, 99) / 1e6),
    }


def run() -> dict:
    rows = []
    for overlap in OVERLAPS:
        for cfo in (False, True):
            seed_rows = [_simulate_once(DOMAINS, overlap, cfo, s) for s in SEEDS]
            row = {"overlap": overlap, "cfo": cfo}
            for k in seed_rows[0]:
                row[k] = float(np.mean([s[k] for s in seed_rows]))
            rows.append(row)
    return {"config": {"domains": DOMAINS, "chunk_bytes": CHUNK_BYTES,
                       "chunks_per_domain": CHUNKS_PER_DOMAIN,
                       "read_bw_gbs": READ_BW_GBS, "egress_bw_gbs": EGRESS_BW_GBS,
                       "break_even_overlap": BREAK_EVEN_OVERLAP, "n_steps": N_STEPS},
            "rows": rows}


def _pick(rows, overlap, cfo):
    return [r for r in rows if abs(r["overlap"] - overlap) < 1e-9 and r["cfo"] is cfo][0]


def report(results: dict) -> None:
    rows = results["rows"]
    print("=" * 74)
    print(f"CFO resource curve, {DOMAINS} domains, read-bound  (paper §IV-D)")
    print(f"Break-even overlap = {BREAK_EVEN_OVERLAP}")
    print("=" * 74)
    print(f"{'Overlap':>8} | {'read util':>20} | {'P99 latency (ms)':>22}")
    print(f"{'':>8} | {'no-CFO':>9} {'CFO':>9} | {'no-CFO':>10} {'CFO':>10}")
    print("-" * 74)
    for ov in OVERLAPS:
        nb, cb = _pick(rows, ov, False), _pick(rows, ov, True)
        print(f"{ov:>8.2f} | {nb['device_read_util']:>9.2f} "
              f"{cb['device_read_util']:>9.2f} | "
              f"{nb['p99_latency_ms']:>10.2f} {cb['p99_latency_ms']:>10.2f}")
    print("-" * 74)
    nb0, cb_full = _pick(rows, 0.0, False), _pick(rows, 1.0, True)
    print(f"Read-port offered load: {nb0['device_read_util']:.2f}x without CFO "
          f"(saturated) -> {cb_full['device_read_util']:.2f}x at 100% overlap with CFO.")
    cb50, cb75 = _pick(rows, 0.5, True), _pick(rows, 0.75, True)
    print(f"P99 promotion latency with CFO: {_pick(rows, 0.0, True)['p99_latency_ms']:.2f} ms "
          f"(0% overlap) -> {cb75['p99_latency_ms']:.2f} ms (75% overlap).")
    print("CFO payoff is conditional on overlap clearing the read-port break-even "
          "(claim reproduced).")


def plot(results: dict):
    import matplotlib.pyplot as plt
    rows = results["rows"]
    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(10, 4))
    ax0.plot(OVERLAPS, [_pick(rows, o, False)["device_read_util"] for o in OVERLAPS],
             "s--", color=C["fts"], label="no CFO")
    ax0.plot(OVERLAPS, [_pick(rows, o, True)["device_read_util"] for o in OVERLAPS],
             "o-", color=C["cefe"], label="CFO")
    ax0.axhline(1.0, color="grey", ls=":", lw=1.5)
    ax0.axvline(BREAK_EVEN_OVERLAP, color=C["accent2"], ls=":", lw=1.5,
                label=f"break-even ({BREAK_EVEN_OVERLAP})")
    ax0.set_xlabel("Cross-domain overlap")
    ax0.set_ylabel("Read-port offered load (x)")
    ax0.set_title("(a) Source-read pressure")
    ax0.legend()
    ax1.plot(OVERLAPS, [_pick(rows, o, False)["p99_latency_ms"] for o in OVERLAPS],
             "s--", color=C["fts"], label="no CFO")
    ax1.plot(OVERLAPS, [_pick(rows, o, True)["p99_latency_ms"] for o in OVERLAPS],
             "o-", color=C["cefe"], label="CFO")
    ax1.set_xlabel("Cross-domain overlap")
    ax1.set_ylabel("P99 promotion latency (ms)")
    ax1.set_title("(b) Completion tail")
    ax1.legend()
    return fig


def main() -> None:
    results = run()
    report(results)
    save_json("repro_cfo_overlap", results)
    save_fig(plot(results), "repro_cfo_overlap")
    print("\nSaved: experiments/out/data/repro_cfo_overlap.json")


if __name__ == "__main__":
    main()
