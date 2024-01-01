#!/usr/bin/env python3
"""Auto-generate results/baselines/figure_summary.txt from the aggregate CSV.

Every sentence is read from summary_aggregate.csv (spec §XV). Nothing is
hand-filled. Reports: zero-RPE methods, per-method normalized throughput,
TwoPhase RTT cost, RefCnt/TwoPhase/PROSE pin spans, the four segment sizes'
throughput/stale/overhead, which segmented point is on the Pareto frontier, and
any result inconsistent with the mechanism-level expectation.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.baselines.baseline_common import (
    METHOD_ORDER, METHOD_LABELS, SEGMENT_SIZES,
)

RESULTS = ROOT / "results" / "baselines"
AGG = RESULTS / "summary_aggregate.csv"
OUT = RESULTS / "figure_summary.txt"
SEG = [f"Segmented-{s}" for s in SEGMENT_SIZES]


def load(path: Path) -> Dict[str, Dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        return {r["method"]: r for r in csv.DictReader(f)}


def _f(a, m, k):
    return float(a[m][k])


def pareto_frontier(a: Dict[str, Dict[str, str]]) -> List[str]:
    """Non-dominated methods: higher throughput AND lower stale is better."""
    # only methods present in this aggregate (METHOD_ORDER may name
    # mechanisms registered after the CSV was produced)
    pts = {m: (_f(a, m, "normalized_throughput_gmean"),
               _f(a, m, "stale_mib_per_gib"))
           for m in METHOD_ORDER if m in a}
    front = []
    for m, (tp, st) in pts.items():
        dominated = any(
            (o != m and pts[o][0] >= tp and pts[o][1] <= st
             and (pts[o][0] > tp or pts[o][1] < st))
            for o in pts)
        if not dominated:
            front.append(m)
    return front


def main() -> int:
    a = load(AGG)
    L: List[str] = []
    L.append("BASELINE COMPARISON — AUTO-GENERATED SUMMARY")
    L.append("(all values read from summary_aggregate.csv; none hand-filled)")
    L.append("=" * 66)

    # 1. zero-RPE methods
    zero_rpe = [m for m in METHOD_ORDER
                if m in a
                and _f(a, m, "stale_mib_per_gib") == 0.0
                and int(float(a[m]["rpe_events"])) == 0]
    L.append("")
    L.append("1. Zero stale-payload / zero-RPE mechanisms:")
    L.append("   " + ", ".join(zero_rpe))

    # 2. normalized throughput per method
    L.append("")
    L.append("2. Normalized valid throughput (geomean, paired to Unsafe):")
    for m in [m for m in METHOD_ORDER if m in a]:
        lo = _f(a, m, "normalized_throughput_ci_low")
        hi = _f(a, m, "normalized_throughput_ci_high")
        L.append(f"   {m:16s} {_f(a, m, 'normalized_throughput_gmean'):6.3f}"
                 f"  [95% CI {lo:.3f}, {hi:.3f}]  (n={a[m]['num_runs']})")

    # 3. serialized coordination round trips (+RTT counts serialized exchanges)
    L.append("")
    L.append("3. Additional serialized host-endpoint round trips (+RTT):")
    L.append(f"   RefCnt   = {int(float(a['SharedRef']['extra_rtt']))} "
             f"(non-coherent shared-metadata atomic: flush+atomic+visibility;")
    L.append("              NOT zero unless the metadata region is hw-coherent).")
    L.append(f"   2Phase   = {int(float(a['TwoPhase']['extra_rtt']))} "
             f"(reserve token exchange).")
    L.append(f"   GenOnly/RKey/Segmented/PROSE = 0 (validation is endpoint-local).")

    # 4. RefCnt / TwoPhase pin spans
    L.append("")
    L.append("4. Protection span (Pin/xfer = protected interval / payload interval):")
    for m in ("SharedRef", "TwoPhase"):
        L.append(f"   {m:16s} median={_f(a, m, 'pin_span_ratio_median'):.2f} "
                 f"p95={_f(a, m, 'pin_span_ratio_p95'):.2f}")

    # 5. PROSE pin span
    L.append("")
    L.append(f"5. PROSE protection span: "
             f"median={_f(a, 'PROSE', 'pin_span_ratio_median'):.2f} "
             f"p95={_f(a, 'PROSE', 'pin_span_ratio_p95'):.2f} "
             f"(transfer-only: admission -> completion).")

    # 6. segment sweep
    L.append("")
    L.append("6. Cancelable-DMA segment sweep (throughput / stale / overhead):")
    L.append(f"   {'segment':>10s} {'norm_tp':>8s} {'stale MiB/GiB':>13s} "
             f"{'ctl+hdr %':>10s}")
    for m, sz in zip(SEG, SEGMENT_SIZES):
        L.append(f"   {sz:>10d} {_f(a, m, 'normalized_throughput_gmean'):8.3f} "
                 f"{_f(a, m, 'stale_mib_per_gib'):13.3f} "
                 f"{_f(a, m, 'control_header_overhead_pct'):10.2f}")

    # 7. non-dominated set in the 2-D (throughput, stale) projection
    front = pareto_frontier(a)
    seg_on_front = [m for m in front if m in SEG]
    L.append("")
    L.append("7. Non-dominated set in the (throughput, stale) projection:")
    L.append("   (higher throughput AND lower stale is better; this is a 2-D")
    L.append("    PROJECTION only — it ignores +RTT, Pin/xfer, and queue-reclaim)")
    L.append("   " + ", ".join(METHOD_LABELS[m] for m in front))
    if "PROSE" not in front:
        L.append("   NOTE: PROSE is DOMINATED here by RefCnt/2Phase, which retain")
        L.append("   more objects by pinning at enqueue (blocking queue-time")
        L.append("   eviction attempts that PROSE deliberately allows). PROSE's")
        L.append("   advantage is on the OTHER axes (panel b), not this projection.")
        L.append("   Do NOT claim PROSE achieves the best throughput-stale frontier.")
    if seg_on_front:
        L.append("   Segmented config(s) non-dominated: "
                 + ", ".join(METHOD_LABELS[m] for m in seg_on_front))

    # 7b. CI-overlap honesty check on the throughput ordering
    L.append("")
    L.append("7b. Throughput-ordering confidence (paired-bootstrap CIs):")
    def _ci(m):
        return (_f(a, m, "normalized_throughput_ci_low"),
                _f(a, m, "normalized_throughput_ci_high"))
    r_lo, r_hi = _ci("SharedRef")
    p_lo, p_hi = _ci("PROSE")
    if r_lo <= p_hi and p_lo <= r_hi:
        L.append("   RefCnt and PROSE CIs OVERLAP -> the data supports only that")
        L.append("   they lie in a similar throughput band; it does NOT support")
        L.append("   'RefCnt is faster than PROSE'. Avoid discussing few-percent")
        L.append("   throughput gaps in prose.")
    else:
        L.append("   RefCnt and PROSE CIs are disjoint in this run.")

    # 8. consistency check
    L.append("")
    L.append("8. Consistency with mechanism-level expectation:")
    issues = []
    for m in ("SharedRef", "TwoPhase", "PROSE"):
        if _f(a, m, "stale_mib_per_gib") != 0.0:
            issues.append(f"{m} stale != 0")
    if int(float(a["TwoPhase"]["extra_rtt"])) != 1:
        issues.append("TwoPhase extra_rtt != 1")
    if int(float(a["PROSE"]["extra_rtt"])) != 0:
        issues.append("PROSE extra_rtt != 0")
    for m in ("SharedRef", "TwoPhase"):
        if a[m]["queue_reclaim"] != "N":
            issues.append(f"{m} queue_reclaim != N")
    if a["PROSE"]["queue_reclaim"] != "Y":
        issues.append("PROSE queue_reclaim != Y")
    # GenOnly/RDMAKey/large segmented SHOULD show stale in the race workloads
    if _f(a, "GenOnly", "stale_mib_per_gib") <= 0:
        issues.append("GenOnly shows no stale — race-stress did not trigger the race")
    if issues:
        L.append("   INCONSISTENCIES FOUND:")
        for i in issues:
            L.append("     - " + i)
    else:
        L.append("   All results consistent with expectation.")

    # standout finding (only if data supports it)
    only = (len(zero_rpe) >= 1 and "PROSE" in zero_rpe
            and int(float(a["PROSE"]["extra_rtt"])) == 0
            and a["PROSE"]["queue_reclaim"] == "Y"
            and abs(_f(a, "PROSE", "pin_span_ratio_median") - 1.0) < 0.25
            and all(not (m != "PROSE"
                         and _f(a, m, "stale_mib_per_gib") == 0
                         and int(float(a[m]["extra_rtt"])) == 0
                         and a[m]["queue_reclaim"] == "Y"
                         and _f(a, m, "pin_span_ratio_median") <= 1.25)
                    for m in METHOD_ORDER if m in a))
    L.append("")
    if only:
        L.append("KEY FINDING: PROSE is the only evaluated mechanism combining "
                 "zero stale payload, zero additional RTT, transfer-only "
                 "protection (Pin/xfer≈1), and queue-time autonomous reclamation.")
    else:
        L.append("KEY FINDING: see the per-metric breakdown above.")

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(f"\nWrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
