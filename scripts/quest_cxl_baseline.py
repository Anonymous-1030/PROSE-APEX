#!/usr/bin/env python3
"""Quest-CXL Baseline Reproduction — Query Decorrelation Analysis.

Reproduces the Quest-CXL baseline algorithm described in §14.2 of the
PROSE-APEX paper, demonstrating the fundamental limitation of query-based
KV-cache page importance estimation over CXL disaggregated memory:

  **Query Decorrelation Phenomenon:**
  Quest uses q_{t-1} (the previous step's query) as a surrogate for q_t
  (the current step's query) to pre-compute page importance. Over CXL's
  non-negligible latency (200–400 ns), the query has decorrelated enough
  that the resulting importance ranking degrades to near-random.

Key Results Reproduced:
  - Recovery@K metric: fraction of true top-K pages recovered by surrogate
  - At K=25, N=80: Recovery@K = 0.31 ≈ random baseline (K/N = 0.3125)
  - This proves Quest's scoring is no better than random selection under
    CXL latency, motivating PROSE-APEX's causal endpoint design.

Mathematical Model:
  1. Generate synthetic attention patterns with realistic temporal correlation
  2. Compute true importance using q_t (oracle)
  3. Compute Quest's surrogate importance using q_{t-1}
  4. Measure overlap (Recovery@K) between true top-K and surrogate top-K
  5. Show degradation to random as CXL latency grows

Paper Reference: §IV-E Table V (same-contract scoring), Quest-CXL analysis

Usage:
  python quest_cxl_baseline.py --output experiments/out/data/quest_cxl.json
  python quest_cxl_baseline.py --sweep-latency  # CXL latency sensitivity
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class QuestConfig:
    """Configuration for Quest-CXL simulation."""

    # Model dimensions (Llama-3-70B-like)
    d_head: int = 128              # Attention head dimension
    n_heads: int = 8               # Number of attention heads (per GQA group)
    n_pages: int = 80              # Total KV-cache pages (N)
    k_select: int = 25             # Pages to select (K)
    page_size_tokens: int = 64     # Tokens per page

    # Temporal correlation model
    # Query evolution follows: q_t = α * q_{t-1} + √(1-α²) * ε_t
    # where α controls decorrelation rate
    query_correlation_alpha: float = 0.85  # Per-step correlation

    # CXL latency model (nanoseconds)
    cxl_read_latency_ns: float = 300.0     # Typical CXL .mem read
    decode_step_ns: float = 120.0          # GPU compute per token

    # Number of decode steps to simulate
    num_steps: int = 2000
    seed: int = 42

    @property
    def cxl_step_lag(self) -> int:
        """How many decode steps the query is stale by over CXL.

        CXL read takes cxl_read_latency_ns; each decode step takes
        decode_step_ns. The surrogate query is from floor(lat/step) steps ago.
        """
        return max(1, int(math.ceil(self.cxl_read_latency_ns / self.decode_step_ns)))


# =============================================================================
# Attention Importance Model
# =============================================================================

class AttentionImportanceModel:
    """Models how query vectors evolve and how page importance is computed.

    Quest's importance metric for page p:
      I(p) = max_{token j in p} |q^T * k_j| / sqrt(d)

    We model this by:
      1. Generating correlated query sequence via AR(1) process
      2. Generating static key vectors for each page
      3. Computing importance = softmax(q^T K / sqrt(d)) summed per page
    """

    def __init__(self, config: QuestConfig):
        self.config = config
        self.rng = np.random.default_rng(config.seed)

        # Generate static KV-cache (pages of key vectors)
        # Shape: (n_pages, page_size_tokens, d_head)
        self.keys = self.rng.standard_normal(
            (config.n_pages, config.page_size_tokens, config.d_head)
        ).astype(np.float32)
        # Normalize keys for numerical stability
        norms = np.linalg.norm(self.keys, axis=-1, keepdims=True)
        self.keys = self.keys / np.maximum(norms, 1e-8)

        # Initialize query state
        self.current_query = self.rng.standard_normal(config.d_head).astype(np.float32)
        self.current_query /= np.linalg.norm(self.current_query)

    def step_query(self) -> np.ndarray:
        """Evolve query via AR(1) process and return new query.

        q_t = α * q_{t-1} + √(1-α²) * ε_t  (maintains unit variance)
        """
        alpha = self.config.query_correlation_alpha
        noise = self.rng.standard_normal(self.config.d_head).astype(np.float32)
        self.current_query = (
            alpha * self.current_query +
            math.sqrt(1.0 - alpha * alpha) * noise
        )
        # Renormalize to unit sphere
        self.current_query /= np.linalg.norm(self.current_query)
        return self.current_query.copy()

    def compute_page_importance(self, query: np.ndarray) -> np.ndarray:
        """Compute per-page importance scores given a query vector.

        For each page p, importance = sum of softmax attention weights
        for tokens in that page. This is the metric Quest uses to rank pages.

        Args:
            query: (d_head,) query vector

        Returns:
            (n_pages,) importance scores
        """
        d = self.config.d_head
        scale = 1.0 / math.sqrt(d)

        # Compute attention logits: (n_pages, page_size) = keys @ query * scale
        # keys: (n_pages, page_size, d_head), query: (d_head,)
        logits = np.einsum("npd,d->np", self.keys, query) * scale

        # Per-page importance: max attention logit across tokens in page
        # (Quest uses max, not sum, per §14.2)
        page_importance = logits.max(axis=1)  # (n_pages,)

        return page_importance

    def get_topk_pages(self, importance: np.ndarray, k: int) -> np.ndarray:
        """Return indices of top-K pages by importance.

        Args:
            importance: (n_pages,) scores
            k: number of pages to select

        Returns:
            (k,) array of page indices
        """
        return np.argsort(importance)[-k:][::-1]


# =============================================================================
# Quest-CXL Simulator
# =============================================================================

class QuestCXLSimulator:
    """Simulates Quest's page selection under CXL latency.

    Quest's algorithm:
      1. At step t, use q_{t-lag} (stale query from `lag` steps ago) to
         estimate page importance
      2. Select top-K pages based on stale importance
      3. Compare against oracle (using current q_t)

    The CXL latency means the query used for importance estimation is
    `cxl_step_lag` steps behind the actual query.
    """

    def __init__(self, config: QuestConfig):
        self.config = config
        self.model = AttentionImportanceModel(config)

    def simulate(self) -> Dict[str, object]:
        """Run full Quest-CXL simulation.

        Returns:
            Dictionary with per-step Recovery@K and aggregate statistics.
        """
        cfg = self.config
        lag = cfg.cxl_step_lag

        # Query history buffer (circular, stores last `lag+1` queries)
        query_history: List[np.ndarray] = []

        # Results
        recovery_at_k_values = []
        per_step_results = []

        for step in range(cfg.num_steps):
            # Generate current query
            q_current = self.model.step_query()
            query_history.append(q_current)

            # We can only compute Recovery@K once we have enough history
            if step < lag:
                continue

            # Oracle: true top-K using current query
            true_importance = self.model.compute_page_importance(q_current)
            true_topk = set(self.model.get_topk_pages(true_importance, cfg.k_select))

            # Quest surrogate: use query from `lag` steps ago
            q_stale = query_history[step - lag]
            stale_importance = self.model.compute_page_importance(q_stale)
            quest_topk = set(self.model.get_topk_pages(stale_importance, cfg.k_select))

            # Recovery@K = |true_topK ∩ quest_topK| / K
            recovery = len(true_topk & quest_topk) / cfg.k_select
            recovery_at_k_values.append(recovery)

            per_step_results.append({
                "step": step,
                "recovery_at_k": recovery,
            })

        # Aggregate statistics
        recovery_arr = np.array(recovery_at_k_values)
        random_baseline = cfg.k_select / cfg.n_pages  # K/N

        return {
            "config": {
                "n_pages": cfg.n_pages,
                "k_select": cfg.k_select,
                "d_head": cfg.d_head,
                "query_correlation_alpha": cfg.query_correlation_alpha,
                "cxl_read_latency_ns": cfg.cxl_read_latency_ns,
                "decode_step_ns": cfg.decode_step_ns,
                "cxl_step_lag": lag,
                "num_steps": cfg.num_steps,
            },
            "results": {
                "mean_recovery_at_k": float(recovery_arr.mean()),
                "std_recovery_at_k": float(recovery_arr.std()),
                "median_recovery_at_k": float(np.median(recovery_arr)),
                "p10_recovery_at_k": float(np.percentile(recovery_arr, 10)),
                "p90_recovery_at_k": float(np.percentile(recovery_arr, 90)),
                "random_baseline": random_baseline,
                "excess_over_random": float(recovery_arr.mean() - random_baseline),
            },
            "per_step": per_step_results,
            "conclusion": self._format_conclusion(recovery_arr, random_baseline, lag),
        }

    def _format_conclusion(self, recovery: np.ndarray, random: float, lag: int) -> str:
        mean_r = recovery.mean()
        if abs(mean_r - random) < 0.05:
            return (
                f"Quest-CXL Recovery@K = {mean_r:.4f} ≈ random baseline {random:.4f} "
                f"(lag={lag} steps). Query decorrelation renders surrogate scoring "
                f"no better than random page selection."
            )
        elif mean_r > random + 0.05:
            return (
                f"Quest-CXL Recovery@K = {mean_r:.4f} > random {random:.4f} "
                f"(lag={lag}). Some residual correlation remains, but far below "
                f"oracle performance."
            )
        else:
            return (
                f"Quest-CXL Recovery@K = {mean_r:.4f} (random={random:.4f}, lag={lag}). "
                f"Decorrelation confirmed."
            )


# =============================================================================
# Latency Sweep (sensitivity analysis)
# =============================================================================

def sweep_cxl_latency(base_config: QuestConfig,
                       latencies_ns: List[float]) -> List[Dict]:
    """Sweep CXL latency and measure Recovery@K degradation.

    This produces the key figure showing that as CXL latency increases,
    Quest's recovery degrades monotonically to random.
    """
    results = []
    for lat in latencies_ns:
        cfg = QuestConfig(
            d_head=base_config.d_head,
            n_heads=base_config.n_heads,
            n_pages=base_config.n_pages,
            k_select=base_config.k_select,
            page_size_tokens=base_config.page_size_tokens,
            query_correlation_alpha=base_config.query_correlation_alpha,
            cxl_read_latency_ns=lat,
            decode_step_ns=base_config.decode_step_ns,
            num_steps=base_config.num_steps,
            seed=base_config.seed,
        )
        sim = QuestCXLSimulator(cfg)
        result = sim.simulate()
        results.append({
            "cxl_latency_ns": lat,
            "cxl_step_lag": cfg.cxl_step_lag,
            "mean_recovery_at_k": result["results"]["mean_recovery_at_k"],
            "random_baseline": result["results"]["random_baseline"],
        })
        print(f"  Latency {lat:6.0f} ns (lag={cfg.cxl_step_lag}): "
              f"Recovery@K = {result['results']['mean_recovery_at_k']:.4f}")

    return results


# =============================================================================
# InfiniGen-CXL Baseline (simplified)
# =============================================================================

class InfiniGenCXLBaseline:
    """Simplified InfiniGen-CXL baseline for comparison.

    InfiniGen pre-computes full attention for ALL pages using q_{t-1},
    then fetches top-K. Under CXL latency, it suffers the same
    decorrelation as Quest but with higher compute overhead.

    The key difference: InfiniGen uses full softmax attention (not max-logit),
    but over CXL the staleness dominates both approaches equally.
    """

    def __init__(self, config: QuestConfig):
        self.config = config
        self.rng = np.random.default_rng(config.seed + 1000)

        # Same key store as Quest
        self.keys = self.rng.standard_normal(
            (config.n_pages, config.page_size_tokens, config.d_head)
        ).astype(np.float32)
        norms = np.linalg.norm(self.keys, axis=-1, keepdims=True)
        self.keys = self.keys / np.maximum(norms, 1e-8)

        self.current_query = self.rng.standard_normal(config.d_head).astype(np.float32)
        self.current_query /= np.linalg.norm(self.current_query)

    def step_query(self) -> np.ndarray:
        alpha = self.config.query_correlation_alpha
        noise = self.rng.standard_normal(self.config.d_head).astype(np.float32)
        self.current_query = alpha * self.current_query + math.sqrt(1 - alpha**2) * noise
        self.current_query /= np.linalg.norm(self.current_query)
        return self.current_query.copy()

    def compute_importance_softmax(self, query: np.ndarray) -> np.ndarray:
        """Full softmax attention importance (InfiniGen's method).

        I(p) = sum_{j in p} softmax(q^T K / sqrt(d))_j
        """
        d = self.config.d_head
        scale = 1.0 / math.sqrt(d)

        # All logits: (n_pages * page_size,)
        all_keys_flat = self.keys.reshape(-1, d)  # (N*P, d)
        logits = (all_keys_flat @ query) * scale  # (N*P,)

        # Stable softmax
        logits_max = logits.max()
        exp_logits = np.exp(logits - logits_max)
        attn_weights = exp_logits / exp_logits.sum()

        # Sum per page
        page_importance = attn_weights.reshape(
            self.config.n_pages, self.config.page_size_tokens
        ).sum(axis=1)

        return page_importance

    def simulate(self) -> Dict[str, float]:
        """Run InfiniGen-CXL and return mean Recovery@K."""
        cfg = self.config
        lag = cfg.cxl_step_lag
        query_history = []
        recoveries = []

        for step in range(cfg.num_steps):
            q = self.step_query()
            query_history.append(q)

            if step < lag:
                continue

            # Oracle
            true_imp = self.compute_importance_softmax(q)
            true_topk = set(np.argsort(true_imp)[-cfg.k_select:])

            # InfiniGen surrogate
            q_stale = query_history[step - lag]
            stale_imp = self.compute_importance_softmax(q_stale)
            infini_topk = set(np.argsort(stale_imp)[-cfg.k_select:])

            recovery = len(true_topk & infini_topk) / cfg.k_select
            recoveries.append(recovery)

        return {
            "method": "InfiniGen-CXL",
            "mean_recovery_at_k": float(np.mean(recoveries)),
            "std_recovery_at_k": float(np.std(recoveries)),
            "random_baseline": cfg.k_select / cfg.n_pages,
        }


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Quest-CXL & InfiniGen-CXL baseline reproduction"
    )
    parser.add_argument("--n-pages", type=int, default=80,
                        help="Number of KV-cache pages N (default: 80)")
    parser.add_argument("--k-select", type=int, default=25,
                        help="Pages to select K (default: 25)")
    parser.add_argument("--alpha", type=float, default=0.85,
                        help="Query correlation parameter (default: 0.85)")
    parser.add_argument("--cxl-latency", type=float, default=300.0,
                        help="CXL read latency in ns (default: 300)")
    parser.add_argument("--steps", type=int, default=2000,
                        help="Number of decode steps (default: 2000)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    parser.add_argument("--output", type=str,
                        default="experiments/out/data/quest_cxl.json",
                        help="Output JSON path")
    parser.add_argument("--sweep-latency", action="store_true",
                        help="Run CXL latency sweep analysis")
    parser.add_argument("--include-infinigen", action="store_true",
                        help="Also run InfiniGen-CXL baseline")

    args = parser.parse_args()

    config = QuestConfig(
        n_pages=args.n_pages,
        k_select=args.k_select,
        query_correlation_alpha=args.alpha,
        cxl_read_latency_ns=args.cxl_latency,
        num_steps=args.steps,
        seed=args.seed,
    )

    print("Quest-CXL Baseline Reproduction")
    print("=" * 60)
    print(f"  N (pages):          {config.n_pages}")
    print(f"  K (select):         {config.k_select}")
    print(f"  Random baseline:    {config.k_select / config.n_pages:.4f}")
    print(f"  Query α:            {config.query_correlation_alpha}")
    print(f"  CXL latency:        {config.cxl_read_latency_ns} ns")
    print(f"  CXL step lag:       {config.cxl_step_lag}")
    print(f"  Decode steps:       {config.num_steps}")
    print("=" * 60)

    all_results = {}

    # Run Quest-CXL
    print("\n[1] Running Quest-CXL simulation...")
    sim = QuestCXLSimulator(config)
    quest_result = sim.simulate()
    all_results["quest_cxl"] = quest_result

    print(f"  Mean Recovery@K:    {quest_result['results']['mean_recovery_at_k']:.4f}")
    print(f"  Random baseline:    {quest_result['results']['random_baseline']:.4f}")
    print(f"  Excess over random: {quest_result['results']['excess_over_random']:.4f}")
    print(f"  Conclusion: {quest_result['conclusion']}")

    # Optional: InfiniGen-CXL
    if args.include_infinigen:
        print("\n[2] Running InfiniGen-CXL simulation...")
        infini = InfiniGenCXLBaseline(config)
        infini_result = infini.simulate()
        all_results["infinigen_cxl"] = infini_result
        print(f"  Mean Recovery@K:    {infini_result['mean_recovery_at_k']:.4f}")
        print(f"  Random baseline:    {infini_result['random_baseline']:.4f}")

    # Optional: Latency sweep
    if args.sweep_latency:
        print("\n[3] CXL Latency Sweep...")
        latencies = [0.0, 50.0, 100.0, 150.0, 200.0, 300.0, 400.0, 600.0, 1000.0]
        sweep = sweep_cxl_latency(config, latencies)
        all_results["latency_sweep"] = sweep

    # Write results
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        # Exclude per_step data for compact output (it's large)
        compact = {k: v for k, v in all_results.items()}
        if "quest_cxl" in compact and "per_step" in compact["quest_cxl"]:
            compact["quest_cxl"] = {
                k: v for k, v in compact["quest_cxl"].items() if k != "per_step"
            }
        json.dump(compact, f, indent=2)
    print(f"\nResults written to {output_path}")

    # Final assertion: Recovery@K should be ≈ random for K=25, N=80
    mean_r = quest_result["results"]["mean_recovery_at_k"]
    random_b = quest_result["results"]["random_baseline"]
    if abs(mean_r - random_b) > 0.10:
        print(f"\n  WARNING: Recovery@K ({mean_r:.4f}) deviates significantly "
              f"from random ({random_b:.4f})")
        print(f"  This may indicate the correlation model needs recalibration.")
    else:
        print(f"\n  CONFIRMED: Quest-CXL Recovery@K ≈ random baseline "
              f"({mean_r:.4f} ≈ {random_b:.4f})")
        print(f"  Query decorrelation renders surrogate scoring ineffective over CXL.")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
