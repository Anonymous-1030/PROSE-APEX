#!/usr/bin/env python3
"""GenOnly + epoch-fence baseline sweep (paper TODO: replace the inherited
GenOnly figure with a measured value).

Replays the FULL 11-method paired sweep — the original 10 methods plus
GenOnlyEpochFence — over the SAME config and seeds as the committed
``results/baselines/summary_by_run.csv``, so the new mechanism's numbers are
directly comparable to the existing table (identical shared traces).

Outputs (NEW files only; nothing committed is overwritten):
  results/baselines_epochfence/summary_by_run.csv        run-level (all 11 methods)
  results/baselines_epochfence/raw/*.jsonl               request-level records
  results/baselines_epochfence/manifest.json             provenance
  results/baselines/summary_aggregate_with_epochfence.csv  paper-level aggregate

As a comparability check the driver also asserts that the 10 inherited methods
reproduce the committed summary_by_run.csv byte-for-byte on the compared
columns (the replay is deterministic given the shared trace).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.baselines.run_baseline_sweep import (  # noqa: E402
    DEFAULT_CFG, run_sweep,
)
from experiments.baselines.aggregate_baselines import (  # noqa: E402
    aggregate, load_runs, write_csv,
)

NEW_DIR = ROOT / "results" / "baselines_epochfence"
COMMITTED_CSV = ROOT / "results" / "baselines" / "summary_by_run.csv"
OUT_AGG = ROOT / "results" / "baselines" / "summary_aggregate_with_epochfence.csv"

COMPARE_METHODS = ("GenOnly", "GenOnlyEpochFence", "PROSE")


def check_reproduction(new_csv: Path) -> int:
    """Assert the 10 inherited methods match the committed run-level CSV."""
    old = {(r["workload"], r["seed"], r["method"]): r
           for r in load_runs(new_csv)}
    mismatches = 0
    with COMMITTED_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["workload"], row["seed"], row["method"])
            new = old.get(key)
            if new is None:
                print(f"MISSING in rerun: {key}")
                mismatches += 1
                continue
            for col in ("makespan_ns", "total_valid_bytes", "total_stale_bytes",
                        "valid_throughput_gbps", "stale_mib_per_gib",
                        "rpe_events", "rejected_requests"):
                if abs(float(new[col]) - float(row[col])) > 1e-9:
                    print(f"MISMATCH {key} {col}: committed={row[col]} rerun={new[col]}")
                    mismatches += 1
    print(f"reproduction check vs committed summary_by_run.csv: "
          f"{mismatches} mismatches")
    return mismatches


def print_comparison(agg_rows) -> None:
    by = {r["method"]: r for r in agg_rows}
    print(f"\n{'method':<18} {'norm_tp':>8} {'stale MiB/GiB':>14} "
          f"{'stale CI':>20} {'rpe':>6}")
    for m in COMPARE_METHODS:
        r = by[m]
        print(f"{m:<18} {r['normalized_throughput_gmean']:>8.3f} "
              f"{r['stale_mib_per_gib']:>14.3f} "
              f"[{r['stale_ci_low']:.3f}, {r['stale_ci_high']:.3f}]"
              f" {r['rpe_events']:>6}")


def main() -> int:
    manifest = run_sweep(DEFAULT_CFG, None, None, results_dir=NEW_DIR)
    if manifest["failed_runs"]:
        print("FAILED RUNS:", manifest["failed_runs"])
        return 1

    rc = check_reproduction(NEW_DIR / "summary_by_run.csv")
    if rc:
        print("WARNING: rerun diverged from the committed results — "
              "comparability is NOT established", file=sys.stderr)

    rows = load_runs(NEW_DIR / "summary_by_run.csv")
    agg_rows = aggregate(rows)
    write_csv(agg_rows, OUT_AGG)
    print(f"\nAggregated {len(rows)} runs -> {OUT_AGG}")
    print_comparison(agg_rows)

    # sanity: the fence can only shrink exposure (<= GenOnly), PROSE stays 0,
    # and the fence must still expose a nonzero tail (else the grace period is
    # modeled longer than intended).
    by = {r["method"]: r for r in agg_rows}
    ef, go, pr = (by["GenOnlyEpochFence"], by["GenOnly"], by["PROSE"])
    assert ef["stale_mib_per_gib"] <= go["stale_mib_per_gib"], \
        "fence stale exceeds GenOnly — fence must only shrink the window"
    assert pr["stale_mib_per_gib"] == 0.0, "PROSE must stay exactly 0"
    assert ef["stale_mib_per_gib"] > 0.0, \
        "fence stale is 0 — suspicious; re-examine the grace-period length"
    print("\nsanity: fence <= GenOnly, PROSE == 0, fence > 0 — all hold")
    return 0 if rc == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
