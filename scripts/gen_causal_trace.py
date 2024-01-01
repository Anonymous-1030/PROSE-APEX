#!/usr/bin/env python3
"""Causal State Trace Generator for PROSE-APEX Simulation.

Generates synthetic LLM decode-step descriptor traces that faithfully reproduce
the statistical properties observed in real workloads (Llama-3-70B, Mixtral-8x22B):

  - Jaccard self-correlation ≈ 0.65 between consecutive decode steps
  - Inter-tenant chunk overlap probability ≈ 0.52 (configurable)
  - Zipfian hot-chunk distribution (skewness ≈ 1.2)
  - 16 concurrent tenants with independent Markov chain state
  - Per-step descriptor count K=25 (top-K admission budget)

Mathematical Model:
  Each tenant maintains a "working set" of chunks modeled as a Markov chain.
  At each decode step t:
    1. Retain fraction ρ of previous working set (ρ calibrated for Jaccard ≈ 0.65)
    2. Sample (1-ρ)×K new chunks from Zipfian distribution over chunk pool
    3. Cross-tenant overlap arises naturally from shared Zipfian hotspots
    4. Overlap probability is tuned by controlling the Zipfian skewness and
       the ratio of hot-pool size to total pool size.

  Jaccard(WS_t, WS_{t-1}) = |WS_t ∩ WS_{t-1}| / |WS_t ∪ WS_{t-1}|
  With retention ρ: Jaccard ≈ ρ / (2 - ρ)  →  for Jaccard=0.65, ρ ≈ 0.788

Output Format:
  JSON Lines (.jsonl), one object per decode step per tenant:
  {"step": int, "tenant_id": int, "descriptors": [{"chunk_id": int, "priority": int, "epoch": int}]}

Paper Reference: §IV-A Methodology / trace-to-workload mapping, Table III

Usage:
  python gen_causal_trace.py --tenants 16 --steps 2000 --output trace.jsonl
  python gen_causal_trace.py --tenants 16 --steps 2000 --overlap 0.52 --jaccard 0.65
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set, Dict, Tuple

import numpy as np


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class TraceConfig:
    """Trace generation parameters calibrated to paper claims."""

    num_tenants: int = 16
    num_steps: int = 2000
    k_budget: int = 25              # Descriptors per step per tenant
    chunk_pool_size: int = 8192     # Total unique chunks across all tenants
    hot_pool_size: int = 512        # Chunks in the "hot" zone (Zipfian head)
    zipf_skewness: float = 1.2     # Zipfian distribution parameter
    target_jaccard: float = 0.65   # Target inter-step self-correlation
    target_overlap: float = 0.52   # Target inter-tenant overlap probability
    seed: int = 42
    output_path: str = "trace.jsonl"

    @property
    def retention_rate(self) -> float:
        """Compute chunk retention rate from target Jaccard similarity.

        Jaccard(A, B) = |A ∩ B| / |A ∪ B|
        If we retain ρ fraction: |A ∩ B| = ρ*K, |A ∪ B| = K + (1-ρ)*K = (2-ρ)*K
        Jaccard = ρ / (2 - ρ)  →  ρ = 2*J / (1 + J)
        """
        j = self.target_jaccard
        return 2.0 * j / (1.0 + j)


# =============================================================================
# Zipfian Sampler with Hot-Pool Bias
# =============================================================================

class ZipfianChunkSampler:
    """Generates chunk IDs from a Zipfian distribution with tunable hotspot.

    The chunk pool is divided into:
      - Hot pool (first `hot_pool_size` chunks): high probability mass
      - Cold pool (remaining): long tail

    This naturally produces inter-tenant overlap because multiple tenants
    independently sample from the same hot pool.
    """

    def __init__(self, pool_size: int, hot_size: int, skewness: float,
                 target_overlap: float, rng: np.random.Generator):
        self.pool_size = pool_size
        self.hot_size = hot_size
        self.skewness = skewness
        self.rng = rng

        # Compute Zipfian PMF over pool_size items
        ranks = np.arange(1, pool_size + 1, dtype=np.float64)
        weights = 1.0 / np.power(ranks, skewness)

        # Boost hot pool probability to achieve target overlap
        # Higher concentration in hot pool → more cross-tenant collisions
        # Calibrate: P(both tenants pick same chunk) ≈ sum(p_i^2) for hot pool
        # We want this ≈ target_overlap * (K/pool_size)
        hot_boost = self._calibrate_hot_boost(weights, hot_size, target_overlap)
        weights[:hot_size] *= hot_boost

        # Normalize
        self.pmf = weights / weights.sum()
        self.chunk_ids = np.arange(pool_size)

    def _calibrate_hot_boost(self, base_weights: np.ndarray, hot_size: int,
                             target_overlap: float) -> float:
        """Binary search for hot-pool boost factor achieving target overlap.

        For two tenants independently sampling K=25 chunks (without replacement)
        from the same PMF p, the expected overlap fraction is:
            overlap = E[|A ∩ B|] / K ≈ K * Σ(p_i²)
        (birthday-paradox approximation, valid when K << N)

        We search for a boost factor that achieves:
            K * Σ(p_i²) ≈ target_overlap

        However, since K=25 is not negligible relative to hot_size=512,
        we use the exact hypergeometric-style correction:
            E[|A ∩ B|] = Σ_i [1 - (1-p_i)^K]^2 * ...
        Simplified: for concentrated distributions, use Monte Carlo calibration
        with a small sample to avoid analytic approximation error.
        """
        # Binary search with Monte Carlo validation
        lo, hi = 1.0, 10000.0
        k = 25
        n_trials = 200  # Quick MC estimate

        for _ in range(40):
            mid = (lo + hi) / 2.0
            w = base_weights.copy()
            w[:hot_size] *= mid
            w /= w.sum()

            # Monte Carlo: draw K items from pmf twice, measure overlap
            overlaps = []
            for _ in range(n_trials):
                a = set(self.rng.choice(len(w), size=k, replace=False, p=w).tolist())
                b = set(self.rng.choice(len(w), size=k, replace=False, p=w).tolist())
                overlaps.append(len(a & b) / k)
            measured = float(np.mean(overlaps))

            if measured < target_overlap:
                lo = mid
            else:
                hi = mid

            # Early termination if close enough
            if abs(measured - target_overlap) < 0.01:
                break

        return (lo + hi) / 2.0

    def sample(self, n: int, exclude: Set[int] = None) -> np.ndarray:
        """Sample n chunk IDs without replacement.

        Args:
            n: number of chunks to sample
            exclude: set of chunk IDs to exclude (already in working set)

        Returns:
            Array of n unique chunk IDs
        """
        if exclude and len(exclude) > 0:
            # Zero out excluded chunks and renormalize
            mask = np.ones(self.pool_size, dtype=np.float64)
            for cid in exclude:
                if 0 <= cid < self.pool_size:
                    mask[cid] = 0.0
            adjusted_pmf = self.pmf * mask
            total = adjusted_pmf.sum()
            if total > 0:
                adjusted_pmf /= total
            else:
                # Fallback: uniform over non-excluded
                adjusted_pmf = mask / mask.sum()
            return self.rng.choice(self.pool_size, size=n, replace=False, p=adjusted_pmf)
        else:
            return self.rng.choice(self.pool_size, size=n, replace=False, p=self.pmf)


# =============================================================================
# Tenant Markov Chain State
# =============================================================================

@dataclass
class TenantState:
    """Per-tenant decode state with Markov working-set evolution."""

    tenant_id: int
    working_set: Set[int] = field(default_factory=set)
    epoch: int = 0
    total_descriptors: int = 0


# =============================================================================
# Trace Generator
# =============================================================================

class CausalTraceGenerator:
    """Main trace generator implementing the Markov working-set model."""

    def __init__(self, config: TraceConfig):
        self.config = config
        self.rng = np.random.default_rng(config.seed)
        self.sampler = ZipfianChunkSampler(
            pool_size=config.chunk_pool_size,
            hot_size=config.hot_pool_size,
            skewness=config.zipf_skewness,
            target_overlap=config.target_overlap,
            rng=self.rng,
        )
        self.tenants = [
            TenantState(tenant_id=i) for i in range(config.num_tenants)
        ]

    def generate(self) -> List[Dict]:
        """Generate the full trace.

        Returns:
            List of step records (one per tenant per step).
        """
        records = []
        retention = self.config.retention_rate
        k = self.config.k_budget

        for step in range(self.config.num_steps):
            for tenant in self.tenants:
                # Evolve working set via Markov retention
                new_ws = self._evolve_working_set(tenant, retention, k)
                tenant.working_set = new_ws
                tenant.epoch = step

                # Generate descriptors from working set
                descriptors = self._make_descriptors(tenant, new_ws, step)
                tenant.total_descriptors += len(descriptors)

                records.append({
                    "step": step,
                    "tenant_id": tenant.tenant_id,
                    "descriptors": descriptors,
                })

        return records

    def _evolve_working_set(self, tenant: TenantState, retention: float,
                            k: int) -> Set[int]:
        """Evolve the tenant's working set for one decode step.

        Two-pool model jointly achieving Jaccard ≈ 0.65 and overlap ≈ 0.52:

        Each tenant's K-chunk working set has two components:
          - SHARED portion (n_shared chunks): drawn from a slowly-rotating
            "global attention set" that all tenants share, providing overlap
          - PRIVATE portion (K - n_shared chunks): unique per-tenant, drawn
            from Zipfian distribution, providing diversity

        The global attention set itself evolves with retention ρ (same as
        the individual working sets), so shared chunks persist across steps,
        contributing to BOTH Jaccard and overlap simultaneously.

        Math:
          n_shared = round(overlap * K) = round(0.52 * 25) = 13
          Jaccard from retained shared: ρ * n_shared / K contributes to Jaccard
          Jaccard from retained private: ρ * n_private / K contributes to Jaccard
          Total Jaccard ≈ ρ / (2 - ρ) ≈ 0.65 ✓ (since both pools use same ρ)
        """
        prev_ws = tenant.working_set

        # Number of shared chunks
        n_shared = int(round(self.config.target_overlap * k))
        n_shared = min(n_shared, k - 1)  # Leave at least 1 private

        # Get the global shared set for this step (managed by generator)
        shared_set = self._get_global_shared_set(tenant.epoch, n_shared)

        # Retention from previous working set's PRIVATE portion
        prev_private = prev_ws - shared_set  # chunks that were private last step
        n_private_total = k - n_shared
        n_retain_private = int(math.floor(retention * n_private_total))
        n_retain_private = min(n_retain_private, len(prev_private))

        if n_retain_private > 0 and len(prev_private) > 0:
            priv_list = list(prev_private)
            self.rng.shuffle(priv_list)
            retained_private = set(priv_list[:n_retain_private])
        else:
            retained_private = set()

        # Fill remaining private slots with fresh Zipfian draws
        n_new_private = n_private_total - len(retained_private)
        exclude = shared_set | retained_private
        if n_new_private > 0:
            new_private = self.sampler.sample(
                min(n_new_private, self.config.chunk_pool_size - len(exclude)),
                exclude=exclude
            )
            private_set = retained_private | set(new_private[:n_new_private].tolist())
        else:
            private_set = retained_private

        # Combine
        new_ws = shared_set | private_set

        # Trim/pad to exactly K
        if len(new_ws) > k:
            ws_list = list(new_ws)
            self.rng.shuffle(ws_list)
            new_ws = set(ws_list[:k])
        elif len(new_ws) < k:
            need = k - len(new_ws)
            extra = self.sampler.sample(need + 5, exclude=new_ws)
            new_ws = new_ws | set(extra[:need].tolist())

        return new_ws

    def _get_global_shared_set(self, step: int, n_shared: int) -> Set[int]:
        """Get the global shared chunk set for a given step.

        The shared set evolves with the same retention rate ρ as individual
        working sets, ensuring temporal correlation. All tenants at the same
        step see the same shared set.

        Uses a separate RNG seeded deterministically per step to ensure
        cross-tenant consistency.
        """
        if not hasattr(self, '_global_shared'):
            self._global_shared = set()
            self._global_shared_step = -1

        # Only update if we've moved to a new step
        if step == self._global_shared_step:
            return self._global_shared

        self._global_shared_step = step
        retention = self.config.retention_rate
        prev_shared = self._global_shared

        # Deterministic RNG for this step (same across all tenants)
        step_rng = np.random.default_rng(self.config.seed + 77777 + step)

        # Retain fraction from previous shared set
        n_retain = int(math.floor(retention * n_shared))
        n_retain = min(n_retain, len(prev_shared))

        if n_retain > 0 and len(prev_shared) > 0:
            shared_list = list(prev_shared)
            step_rng.shuffle(shared_list)
            retained = set(shared_list[:n_retain])
        else:
            retained = set()

        # New shared chunks from hot pool
        n_new = n_shared - len(retained)
        if n_new > 0:
            candidates = [c for c in range(self.config.hot_pool_size) if c not in retained]
            if len(candidates) >= n_new:
                new_ids = step_rng.choice(candidates, size=n_new, replace=False)
                retained = retained | set(new_ids.tolist())
            else:
                retained = retained | set(candidates)

        self._global_shared = retained
        return self._global_shared

    def _make_descriptors(self, tenant: TenantState, ws: Set[int],
                          step: int) -> List[Dict]:
        """Convert working set to descriptor list with priorities.

        Priority assignment:
          - Chunks retained from previous step get higher priority (recency)
          - New chunks get priority based on their Zipfian rank (popularity)
        """
        descriptors = []
        prev_ws = tenant.working_set

        for chunk_id in sorted(ws):
            # Priority: retained chunks get boost
            if chunk_id in prev_ws:
                priority = min(255, 128 + int(self.rng.integers(0, 64)))
            else:
                # New chunk: priority from Zipfian rank
                rank = chunk_id + 1  # 1-indexed rank
                priority = max(1, min(255, int(256.0 / (rank ** 0.3))))

            descriptors.append({
                "chunk_id": int(chunk_id),
                "priority": int(priority),
                "epoch": step,
            })

        return descriptors

    def validate_statistics(self, records: List[Dict]) -> Dict[str, float]:
        """Validate that generated trace matches target statistics.

        Returns dict with measured Jaccard, overlap probability, and other metrics.
        """
        # Measure Jaccard self-correlation (per-tenant, averaged)
        jaccard_values = []
        for tid in range(self.config.num_tenants):
            tenant_records = [r for r in records if r["tenant_id"] == tid]
            for i in range(1, len(tenant_records)):
                prev_chunks = set(d["chunk_id"] for d in tenant_records[i-1]["descriptors"])
                curr_chunks = set(d["chunk_id"] for d in tenant_records[i]["descriptors"])
                if len(prev_chunks | curr_chunks) > 0:
                    j = len(prev_chunks & curr_chunks) / len(prev_chunks | curr_chunks)
                    jaccard_values.append(j)

        # Measure inter-tenant overlap probability
        overlap_values = []
        for step in range(self.config.num_steps):
            step_records = [r for r in records if r["step"] == step]
            for i in range(len(step_records)):
                for j_idx in range(i + 1, len(step_records)):
                    chunks_i = set(d["chunk_id"] for d in step_records[i]["descriptors"])
                    chunks_j = set(d["chunk_id"] for d in step_records[j_idx]["descriptors"])
                    # Overlap = fraction of chunks shared
                    if len(chunks_i) > 0:
                        overlap = len(chunks_i & chunks_j) / len(chunks_i)
                        overlap_values.append(overlap)

        mean_jaccard = float(np.mean(jaccard_values)) if jaccard_values else 0.0
        mean_overlap = float(np.mean(overlap_values)) if overlap_values else 0.0

        # Hot chunk concentration: what fraction of all requests hit top-10% chunks
        all_chunks = [d["chunk_id"] for r in records for d in r["descriptors"]]
        chunk_counts = np.bincount(all_chunks, minlength=self.config.chunk_pool_size)
        top_10_pct = int(0.1 * self.config.chunk_pool_size)
        sorted_counts = np.sort(chunk_counts)[::-1]
        hot_fraction = sorted_counts[:top_10_pct].sum() / max(sorted_counts.sum(), 1)

        return {
            "measured_jaccard": mean_jaccard,
            "target_jaccard": self.config.target_jaccard,
            "jaccard_error": abs(mean_jaccard - self.config.target_jaccard),
            "measured_overlap": mean_overlap,
            "target_overlap": self.config.target_overlap,
            "overlap_error": abs(mean_overlap - self.config.target_overlap),
            "hot_chunk_concentration": hot_fraction,
            "total_records": len(records),
            "total_descriptors": sum(len(r["descriptors"]) for r in records),
        }

    def write_jsonl(self, records: List[Dict], path: str) -> None:
        """Write records to JSON Lines format."""
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate causal LLM decode trace for PROSE-APEX simulation"
    )
    parser.add_argument("--tenants", type=int, default=16,
                        help="Number of concurrent tenants (default: 16)")
    parser.add_argument("--steps", type=int, default=2000,
                        help="Number of decode steps (default: 2000)")
    parser.add_argument("--k-budget", type=int, default=25,
                        help="Descriptors per step per tenant (default: 25)")
    parser.add_argument("--pool-size", type=int, default=8192,
                        help="Total chunk pool size (default: 8192)")
    parser.add_argument("--hot-pool", type=int, default=512,
                        help="Hot pool size (default: 512)")
    parser.add_argument("--zipf-skew", type=float, default=1.2,
                        help="Zipfian skewness (default: 1.2)")
    parser.add_argument("--jaccard", type=float, default=0.65,
                        help="Target Jaccard self-correlation (default: 0.65)")
    parser.add_argument("--overlap", type=float, default=0.52,
                        help="Target inter-tenant overlap probability (default: 0.52)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    parser.add_argument("--output", type=str, default="experiments/out/data/trace.jsonl",
                        help="Output file path")
    parser.add_argument("--validate", action="store_true",
                        help="Run statistical validation after generation")
    parser.add_argument("--validate-only", type=str, default=None,
                        help="Validate an existing trace file")

    args = parser.parse_args()

    config = TraceConfig(
        num_tenants=args.tenants,
        num_steps=args.steps,
        k_budget=args.k_budget,
        chunk_pool_size=args.pool_size,
        hot_pool_size=args.hot_pool,
        zipf_skewness=args.zipf_skew,
        target_jaccard=args.jaccard,
        target_overlap=args.overlap,
        seed=args.seed,
        output_path=args.output,
    )

    print(f"PROSE-APEX Causal Trace Generator")
    print(f"=" * 60)
    print(f"  Tenants:        {config.num_tenants}")
    print(f"  Steps:          {config.num_steps}")
    print(f"  K budget:       {config.k_budget}")
    print(f"  Chunk pool:     {config.chunk_pool_size}")
    print(f"  Hot pool:       {config.hot_pool_size}")
    print(f"  Zipf skewness:  {config.zipf_skewness}")
    print(f"  Target Jaccard: {config.target_jaccard}")
    print(f"  Retention rate: {config.retention_rate:.4f}")
    print(f"  Target overlap: {config.target_overlap}")
    print(f"  Seed:           {config.seed}")
    print(f"  Output:         {config.output_path}")
    print(f"=" * 60)

    generator = CausalTraceGenerator(config)

    if args.validate_only:
        # Load and validate existing trace
        records = []
        with open(args.validate_only) as f:
            for line in f:
                records.append(json.loads(line))
        stats = generator.validate_statistics(records)
    else:
        # Generate trace
        print("\nGenerating trace...")
        records = generator.generate()
        print(f"  Generated {len(records)} records "
              f"({sum(len(r['descriptors']) for r in records)} descriptors)")

        # Write output
        generator.write_jsonl(records, config.output_path)
        print(f"  Written to {config.output_path}")

        # Validate if requested
        if args.validate:
            print("\nValidating statistics (sampling subset for speed)...")
            # Use subset for faster validation
            subset_steps = min(200, config.num_steps)
            subset = [r for r in records if r["step"] < subset_steps]
            sub_config = TraceConfig(
                num_tenants=config.num_tenants,
                num_steps=subset_steps,
                k_budget=config.k_budget,
                chunk_pool_size=config.chunk_pool_size,
                hot_pool_size=config.hot_pool_size,
                zipf_skewness=config.zipf_skewness,
                target_jaccard=config.target_jaccard,
                target_overlap=config.target_overlap,
                seed=config.seed,
            )
            sub_gen = CausalTraceGenerator(sub_config)
            stats = sub_gen.validate_statistics(subset)
        else:
            stats = None

    if stats:
        print(f"\n{'Validation Results':=^60}")
        print(f"  Measured Jaccard:     {stats['measured_jaccard']:.4f} "
              f"(target: {stats['target_jaccard']:.4f}, "
              f"error: {stats['jaccard_error']:.4f})")
        print(f"  Measured Overlap:     {stats['measured_overlap']:.4f} "
              f"(target: {stats['target_overlap']:.4f}, "
              f"error: {stats['overlap_error']:.4f})")
        print(f"  Hot Chunk Conc.:      {stats['hot_chunk_concentration']:.4f}")
        print(f"  Total Descriptors:    {stats['total_descriptors']}")

        # Assertion-style checks
        if stats['jaccard_error'] > 0.10:
            print(f"\n  WARNING: Jaccard error {stats['jaccard_error']:.4f} > 0.10")
            sys.exit(1)
        if stats['overlap_error'] > 0.15:
            print(f"\n  WARNING: Overlap error {stats['overlap_error']:.4f} > 0.15")
            sys.exit(1)
        print(f"\n  All statistical checks PASSED.")

    print("\nDone.")


if __name__ == "__main__":
    main()
