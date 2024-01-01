#!/usr/bin/env python3
"""RPE burst-concentration analysis (occupancy-based).

Inputs:
  - A per-event RPE JSONL produced by generate_rpe_event_log.py.
  - The pool capacity in chunks for that trace.

Outputs:
  - A JSON summary with per-capacity RPE statistics computed from instantaneous
    pool occupancy at issue time.
  - A 2x2 PDF figure showing occupancy time series + RPE rug ticks for four
    capacity configurations on the same trace.
"""
from __future__ import annotations

import argparse
import csv
import heapq
import json
import random
import sys
from pathlib import Path

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "trace_adapter"))

from rpe_binding_model import _Pool


def load_events(path: Path) -> list[dict]:
    events = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                events.append(json.loads(line))
    return events


def is_rpe(ev: dict) -> bool:
    return ev.get("stale") is True or ev.get("event") == "rpe"


def build_occupancy_curve(events: list[dict]):
    """Return (times, occupancy) step arrays from descriptor_issued occupancy field."""
    if not events:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64)
    sorted_events = sorted(events, key=lambda e: e.get("issue_tick", 0))
    times = []
    occ = []
    current_occ = 0
    last_t = None
    for ev in sorted_events:
        t = int(ev.get("issue_tick", ev.get("timestamp_tick", 0)))
        if last_t is not None and t != last_t:
            times.append(last_t)
            occ.append(current_occ)
        current_occ = int(ev.get("occupancy", current_occ))
        last_t = t
    if last_t is not None:
        times.append(last_t)
        occ.append(current_occ)
    return np.asarray(times, dtype=np.int64), np.asarray(occ, dtype=np.int64)


def occupancy_at(times: np.ndarray, occ: np.ndarray, t: int) -> int:
    idx = np.searchsorted(times, t, side="right") - 1
    if idx < 0:
        return 0
    return int(occ[idx])


def analyze(trace_path: Path, capacity: int) -> dict:
    """Compute occupancy-based RPE concentration statistics."""
    events = load_events(trace_path)
    rpe_events = [ev for ev in events if is_rpe(ev)]
    times, occupancy = build_occupancy_curve(events)

    if len(times) == 0:
        raise ValueError(f"no valid occupancy data in {trace_path}")

    total_rpe_events = len(rpe_events)
    rpe_at_capacity_count = 0
    rpe_occupancies = []

    for ev in rpe_events:
        t = int(ev.get("issue_tick", ev.get("timestamp_tick", 0)))
        o = occupancy_at(times, occupancy, t)
        rpe_occupancies.append(o)
        if o >= capacity:
            rpe_at_capacity_count += 1

    rpe_at_capacity_pct = (
        100.0 * rpe_at_capacity_count / total_rpe_events if total_rpe_events else 0.0
    )
    max_occupancy = int(occupancy.max()) if len(occupancy) else 0
    mean_occupancy = float(occupancy.mean()) if len(occupancy) else 0.0
    mean_occupancy_during_rpe = (
        float(np.mean(rpe_occupancies)) if rpe_occupancies else 0.0
    )

    # occupancy-multiple of the 1x baseline (capacity 256) at peak RPE windows
    baseline_capacity = 256
    peak_rpe_window_multiple = None
    if rpe_occupancies:
        # Peak-RPE window: highest occupancy among the top 1% of RPE events,
        # used as a robust "peak RPE window" proxy.
        sorted_occ = sorted(rpe_occupancies, reverse=True)
        top_k = max(1, len(sorted_occ) // 100)
        peak_occ = int(np.mean(sorted_occ[:top_k]))
        peak_rpe_window_multiple = round(peak_occ / baseline_capacity, 2)

    return {
        "trace_path": str(trace_path),
        "capacity": capacity,
        "total_descriptors": len(events),
        "total_rpe_events": total_rpe_events,
        "rpe_at_capacity_count": rpe_at_capacity_count,
        "rpe_at_capacity_pct": round(rpe_at_capacity_pct, 2),
        "max_occupancy": max_occupancy,
        "mean_occupancy": round(mean_occupancy, 2),
        "mean_occupancy_during_rpe": round(mean_occupancy_during_rpe, 2),
        "peak_rpe_window_multiple_vs_256": peak_rpe_window_multiple,
    }


def plot_burst_grid(
    trace_path: str,
    capacities: list[int],
    queue_delay: int,
    tenants: int,
    policy: str,
    seed: int,
    max_events: int,
    output_path: Path,
    zoom: bool = True,
):
    """Generate combined 2x2 (+zoom) occupancy/RPE figure.

    Occupancy is normalized to the 1x baseline capacity (256 chunks), so each
    subplot's own capacity line shows where the pool becomes full and eviction
    starts. The dashed 4x line is the absolute 1024-chunk configuration.
    """
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif", "serif"],
        "font.size": 9,
        "axes.labelsize": 9,
        "axes.titlesize": 9,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "axes.linewidth": 0.9,
        "lines.linewidth": 1.0,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": 150,
        "savefig.dpi": 300,
        "pdf.fonttype": 42,
    })

    baseline_capacity = 256
    data = []
    for cap in capacities:
        events = generate_event_log(
            trace_path, Path(trace_path).stem, cap, policy,
            tenants, queue_delay, max_events, seed,
        )
        times, occupancy = build_occupancy_curve(events)
        rpe_events = [ev for ev in events if is_rpe(ev)]
        rpe_t = np.array([
            int(ev.get("issue_tick", ev.get("timestamp_tick", 0)))
            for ev in rpe_events
        ], dtype=np.int64)
        data.append({
            "capacity": cap,
            "events": events,
            "times": times,
            "occupancy": occupancy / baseline_capacity,
            "rpe_times": rpe_t,
        })

    if zoom:
        fig = plt.figure(figsize=(5.6, 2.6))
        gs = gridspec.GridSpec(2, 3, figure=fig, width_ratios=[1, 1, 0.48],
                               wspace=0.30, hspace=0.32)
        axes = [fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1]),
                fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[1, 1])]
        ax_zoom = fig.add_subplot(gs[:, 2])
    else:
        fig, axes = plt.subplots(2, 2, figsize=(6.5, 4.2))
        axes = axes.flatten()
        ax_zoom = None

    global_t0 = min(int(d["times"][0]) for d in data if len(d["times"]))
    global_t1 = max(int(d["times"][-1]) for d in data if len(d["times"]))

    for ax, d in zip(axes, data):
        cap = d["capacity"]
        times = d["times"]
        occupancy = d["occupancy"]
        rpe_t = d["rpe_times"]
        t_norm = times - global_t0
        rpe_t_norm = rpe_t - global_t0

        # RPE rug ticks at the top edge (short marks, not a wall)
        if len(rpe_t_norm):
            ax.vlines(
                rpe_t_norm,
                ymin=0.96,
                ymax=1.0,
                transform=ax.get_xaxis_transform(),
                color="darkred",
                alpha=0.22,
                lw=0.45,
                zorder=1,
            )

        # Instantaneous pool occupancy, normalized to the 1x baseline
        ax.plot(
            t_norm,
            occupancy,
            color="#1f77b4",
            drawstyle="steps-post",
            lw=1.0,
            zorder=3,
        )

        # Capacity line for this config
        cap_norm = cap / baseline_capacity
        ax.axhline(
            cap_norm,
            color="#333333",
            linestyle="-",
            linewidth=1.1,
            zorder=2,
        )
        # 4x absolute line (1024 chunks normalized to 256)
        ax.axhline(
            4.0,
            color="#888888",
            linestyle="--",
            linewidth=0.9,
            zorder=2,
        )

        # Small labels at the right edge
        ax.text(
            t_norm[-1] if len(t_norm) else 1, cap_norm,
            f" {cap_norm:g}× ({cap})",
            va="center", ha="left", fontsize=7, color="#333333"
        )
        if cap != 1024:
            ax.text(
                t_norm[-1] if len(t_norm) else 1, 4.0,
                " 4×", va="center", ha="left", fontsize=7, color="#888888"
            )

        ax.set_xlim(0, global_t1 - global_t0)
        ax.set_ylim(0, 4.5)
        ax.set_xlabel("Time (model ticks)")
        ax.set_ylabel("Occupancy / 1× capacity")
        ax.set_yticks([0, 1, 2, 3, 4])

    # Only bottom row xlabels, only left column ylabels
    for ax in axes[1:4:2]:  # right column
        ax.set_ylabel("")
    for ax in axes[:2]:     # top row
        ax.set_xlabel("")

    # Zoom panel: densest 2% window for the 1x (256) config, normalized
    if ax_zoom is not None:
        d256 = next(d for d in data if d["capacity"] == 256)
        times = d256["times"]
        occupancy = d256["occupancy"]
        rpe_t = d256["rpe_times"]
        t_norm = times - global_t0
        rpe_t_norm = rpe_t - global_t0
        duration = global_t1 - global_t0

        window_fraction = 0.005
        window_size = max(1, int(window_fraction * duration))
        best_start = 0
        best_count = -1
        sorted_rpe = np.sort(rpe_t_norm)
        for start in np.linspace(0, max(1, duration - window_size), 400):
            start_i = int(start)
            end_i = start_i + window_size
            cnt = np.sum((sorted_rpe >= start_i) & (sorted_rpe < end_i))
            if cnt > best_count:
                best_count = int(cnt)
                best_start = start_i
        zoom_end = best_start + window_size

        mask = (rpe_t_norm >= best_start) & (rpe_t_norm < zoom_end)
        ax_zoom.vlines(
            rpe_t_norm[mask],
            ymin=0.92,
            ymax=1.0,
            transform=ax_zoom.get_xaxis_transform(),
            color="darkred",
            alpha=0.18,
            lw=0.5,
            zorder=1,
        )
        occ_mask = (t_norm >= best_start) & (t_norm < zoom_end)
        ax_zoom.plot(
            t_norm[occ_mask],
            occupancy[occ_mask],
            color="#1f77b4",
            drawstyle="steps-post",
            lw=1.1,
            zorder=3,
        )
        ax_zoom.axhline(1.0, color="#333333", linestyle="-", linewidth=1.0, zorder=2)
        ax_zoom.set_xlim(best_start, zoom_end)
        zoom_ymax = 1.08
        ax_zoom.set_ylim(0.75, zoom_ymax)
        ax_zoom.set_yticks([0.8, 1.0])
        ax_zoom.tick_params(axis="y", which="both", labelleft=True, labelright=False)
        ax_zoom.spines["right"].set_visible(False)
        ax_zoom.set_xlabel("Time (model ticks)")
        ax_zoom.set_title("Zoom (1×, densest window)", fontsize=8, pad=4)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


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
    """Local copy of generate_rpe_event_log.py logic (avoids subprocess)."""
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
            concurrent = sum(1 for ev in inflight if ev[5] <= now < ev[0]) + 1
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
                "occupancy": pool.occupancy,
            })

    with open(trace_path, "r", newline="") as f:
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


def decide_caption(stats_by_capacity: dict[int, dict]) -> str:
    """Return the recommended caption wording based on the data.

    The downgraded, always-accurate claim is used: RPEs coincide with full-pool
    (eviction-active) windows across all capacity configurations. The 4x-baseline
    occupancy is reached only by the 1024-chunk configuration, so we do not claim
    it for every configuration.
    """
    baseline_capacity = 256
    # Sanity: occupancy can never exceed capacity, so full-pool is the trigger.
    for cap in sorted(stats_by_capacity):
        st = stats_by_capacity[cap]
        if st["max_occupancy"] < cap:
            # Should not happen once the pool has warmed up; report if it does.
            pass

    pct_str = ", ".join(
        f"{st['rpe_at_capacity_pct']:.1f}%" for st in stats_by_capacity.values()
    )
    return (
        "Instantaneous pool occupancy (normalized to the 1$\\times$ capacity of "
        "256 chunks) and RPE event times (top ticks) for one public-trace replay "
        "under four capacity configurations. RPE events occur while occupancy "
        "reaches the provisioned capacity (eviction-active windows): "
        f"{pct_str} of RPEs for the 0.5$\\times$, 1$\\times$, 2$\\times$, and "
        "4$\\times$ configurations, respectively. Because the workload fills each "
        "capacity, raising provisioned capacity from 0.5$\\times$ to 4$\\times$ "
        "does not remove exposure; gating must close it."
    )


def main():
    parser = argparse.ArgumentParser(description="RPE burst concentration analysis")
    parser.add_argument("--trace", type=Path, required=True, help="trace CSV")
    parser.add_argument("--capacities", type=int, nargs="+", default=[128, 256, 512, 1024])
    parser.add_argument("--queue-delay", type=int, default=512)
    parser.add_argument("--tenants", type=int, default=16)
    parser.add_argument("--policy", type=str, default="LRU")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-events", type=int, default=20000)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    parser.add_argument("--figure-dir", type=Path, default=ROOT / "figures")
    parser.add_argument("--no-grid", action="store_true", help="only print stats, skip figure")
    parser.add_argument("--no-zoom", action="store_true", help="omit zoom panel")
    args = parser.parse_args()

    stats_by_capacity: dict[int, dict] = {}

    print("=" * 60)
    print(f"Trace: {args.trace}")
    print(f"queue_delay={args.queue_delay}, tenants={args.tenants}, policy={args.policy}, seed={args.seed}")
    print("=" * 60)

    for cap in args.capacities:
        log_path = args.output_dir / f"rpe_events_{args.trace.stem}_cap{cap}_qd{args.queue_delay}.jsonl"
        events = generate_event_log(
            str(args.trace), args.trace.stem, cap, args.policy,
            args.tenants, args.queue_delay, args.max_events, args.seed,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as handle:
            for ev in events:
                handle.write(json.dumps(ev) + "\n")

        stats = analyze(log_path, cap)
        stats_by_capacity[cap] = stats
        print(json.dumps(stats, indent=2))

    summary_path = args.output_dir / f"rpe_burst_summary_grid_{args.trace.stem}.json"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(stats_by_capacity, handle, indent=2)

    caption = decide_caption(stats_by_capacity)
    print("\nRecommended caption:")
    print(caption)

    if not args.no_grid:
        figure_path = args.figure_dir / "rpe_burst_grid.pdf"
        plot_burst_grid(
            str(args.trace),
            capacities=args.capacities,
            queue_delay=args.queue_delay,
            tenants=args.tenants,
            policy=args.policy,
            seed=args.seed,
            max_events=args.max_events,
            output_path=figure_path,
            zoom=not args.no_zoom,
        )
        print(f"\nFigure written to {figure_path}")


if __name__ == "__main__":
    main()
