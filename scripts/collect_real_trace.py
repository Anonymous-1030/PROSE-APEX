#!/usr/bin/env python3
"""Real LLM Attention Trace Collector for PROSE-APEX.

Instantiates Qwen2.5-7B, executes real autoregressive decode steps, and
extracts per-step attention distributions mapped onto 64 KB physical CXL
chunks.  The output format (JSONL + optional .npz) is wire-compatible with
gen_causal_trace.py so downstream experiments consume it unchanged.

Requirements (install via `pip install torch transformers accelerate`):
    torch>=2.1.0
    transformers>=4.36.0
    accelerate>=0.25.0

Hardware:
    - Minimum 1× A100-40GB (bf16, 7B param ~14 GB weights + KV cache)
    - Multi-GPU supported via device_map="auto" (accelerate)
    - CPU offload available with --allow-cpu-offload for <24GB VRAM setups

Usage:
    python scripts/collect_real_trace.py --steps 2000 --output trace_real.jsonl
    python scripts/collect_real_trace.py --steps 500 --npz-out trace_real.npz
    python scripts/collect_real_trace.py --steps 2000 --validate

Paper Reference: §IV-A Methodology / §IV-B RPE under trace-driven replays
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class CollectionConfig:
    """Real trace collection parameters."""

    model_id: str = "Qwen/Qwen2.5-7B"
    num_steps: int = 2000
    k_budget: int = 25                # Top-K chunks per step (matches hw budget)
    chunk_size_bytes: int = 64 * 1024 # 64 KB physical CXL chunk
    num_tenants: int = 16             # Simulated tenants (round-robin heads)
    context_window: int = 32768       # Max context tokens
    warmup_tokens: int = 512          # Prefill length before decode collection
    seed: int = 42
    dtype: str = "bfloat16"
    allow_cpu_offload: bool = False
    output_path: str = "experiments/out/data/trace_real.jsonl"
    npz_path: Optional[str] = None
    validate: bool = False
    prompt_file: Optional[str] = None

    # Derived: tokens per chunk (computed at runtime from model config)
    tokens_per_chunk: int = 0
    total_chunks: int = 0


# =============================================================================
# VRAM Safety Check
# =============================================================================

def check_vram_availability(config: CollectionConfig) -> None:
    """Pre-flight VRAM check. Abort early if obviously insufficient."""
    import torch

    if not torch.cuda.is_available():
        if config.allow_cpu_offload:
            warnings.warn(
                "No CUDA device found. Running with CPU offload — expect "
                "~50x slowdown. Use --allow-cpu-offload to suppress this.",
                RuntimeWarning,
            )
            return
        print("ERROR: No CUDA device detected and --allow-cpu-offload not set.")
        print("       Qwen2.5-7B requires at least one GPU with >=24GB VRAM,")
        print("       or pass --allow-cpu-offload for CPU/disk offload mode.")
        sys.exit(1)

    total_vram = 0
    for i in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(i)
        total_vram += props.total_mem
        print(f"  GPU {i}: {props.name} — {props.total_mem / 1e9:.1f} GB")

    # Qwen2.5-7B in bf16: ~14 GB weights + ~4 GB KV cache (2048 ctx) + overhead
    min_required_gb = 16.0
    total_gb = total_vram / 1e9

    if total_gb < min_required_gb:
        if config.allow_cpu_offload:
            warnings.warn(
                f"Total VRAM ({total_gb:.1f} GB) below recommended "
                f"({min_required_gb:.1f} GB). Enabling CPU offload.",
                RuntimeWarning,
            )
        else:
            print(f"ERROR: Total VRAM ({total_gb:.1f} GB) < minimum "
                  f"({min_required_gb:.1f} GB) for Qwen2.5-7B bf16.")
            print("       Pass --allow-cpu-offload to use CPU/disk offload.")
            sys.exit(1)

    print(f"  Total VRAM: {total_gb:.1f} GB — OK")


# =============================================================================
# Model Loading
# =============================================================================

def load_model(config: CollectionConfig):
    """Load Qwen2.5-7B with eager attention (no SDPA/FlashAttn kernel).

    Returns (model, tokenizer) tuple.  The model is configured to output
    attention weights at every layer via output_attentions=True.
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    dtype_map = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    torch_dtype = dtype_map.get(config.dtype, torch.bfloat16)

    print(f"\nLoading {config.model_id} (dtype={config.dtype}, "
          f"attn_implementation=eager)...")

    # Determine device map
    if config.allow_cpu_offload:
        device_map = "auto"  # accelerate will offload layers to CPU/disk
    elif torch.cuda.device_count() > 1:
        device_map = "auto"  # distribute across GPUs
    else:
        device_map = "auto"  # single GPU, let accelerate handle placement

    tokenizer = AutoTokenizer.from_pretrained(
        config.model_id,
        trust_remote_code=True,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id

    model = AutoModelForCausalLM.from_pretrained(
        config.model_id,
        torch_dtype=torch_dtype,
        device_map=device_map,
        attn_implementation="eager",  # CRITICAL: exposes raw attention weights
        trust_remote_code=True,
    )
    model.eval()

    # Report memory after loading
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            alloc = torch.cuda.memory_allocated(i) / 1e9
            print(f"  GPU {i} memory after load: {alloc:.2f} GB")

    return model, tokenizer


# =============================================================================
# Chunk Mapping: Token Index → Physical Chunk ID
# =============================================================================

@dataclass
class ChunkMapper:
    """Maps logical KV cache token positions to physical 64KB chunk IDs.

    CXL.mem stores KV cache in contiguous 64KB pages.  Each page holds
    `tokens_per_chunk` consecutive token KV pairs for one layer.

    For Qwen2.5-7B:
        hidden_dim = 3584, num_kv_heads = 4, head_dim = 128
        KV per token per layer = 2 × num_kv_heads × head_dim × sizeof(bf16)
                               = 2 × 4 × 128 × 2 = 2048 bytes
        tokens_per_chunk = 64KB / 2048 = 32 tokens/chunk/layer

    The physical layout interleaves layers: chunk_id encodes both the layer
    and the token-range, but for attention-mass scoring we aggregate across
    layers (the hardware does per-layer scoring in parallel and merges).
    """

    tokens_per_chunk: int
    num_layers: int
    total_tokens: int  # current sequence length

    @classmethod
    def from_model_config(cls, model_config, seq_len: int,
                          chunk_bytes: int = 64 * 1024) -> "ChunkMapper":
        """Construct from a HuggingFace model config."""
        # Qwen2.5 uses GQA: num_key_value_heads < num_attention_heads
        num_kv_heads = getattr(model_config, "num_key_value_heads",
                               model_config.num_attention_heads)
        head_dim = getattr(model_config, "head_dim",
                           model_config.hidden_size // model_config.num_attention_heads)
        # Bytes per token per layer: K + V, each [num_kv_heads, head_dim] in bf16
        bytes_per_tok_per_layer = 2 * num_kv_heads * head_dim * 2  # 2 for K+V, 2 for bf16
        tokens_per_chunk = chunk_bytes // bytes_per_tok_per_layer

        return cls(
            tokens_per_chunk=tokens_per_chunk,
            num_layers=model_config.num_hidden_layers,
            total_tokens=seq_len,
        )

    @property
    def num_chunks_per_layer(self) -> int:
        return math.ceil(self.total_tokens / self.tokens_per_chunk)

    @property
    def total_chunks(self) -> int:
        """Total physical chunks across all layers."""
        return self.num_chunks_per_layer * self.num_layers

    def token_to_chunk(self, token_idx: int, layer: int) -> int:
        """Map a (token_idx, layer) pair to a physical chunk ID."""
        chunk_in_layer = token_idx // self.tokens_per_chunk
        return layer * self.num_chunks_per_layer + chunk_in_layer

    def aggregate_attention_to_chunks(
        self,
        attn_weights: "np.ndarray",  # [num_heads, seq_len] for one layer
        layer: int,
    ) -> np.ndarray:
        """Aggregate per-token attention to per-chunk attention mass.

        Args:
            attn_weights: Attention weights from the last query token to all
                          KV positions.  Shape [num_heads, seq_len].
            layer: Layer index for chunk ID computation.

        Returns:
            chunk_scores: [num_chunks_per_layer] attention mass per chunk.
        """
        num_heads, seq_len = attn_weights.shape
        n_chunks = self.num_chunks_per_layer

        # Sum-pool attention across all heads, then aggregate into chunks
        head_avg = attn_weights.mean(axis=0)  # [seq_len]

        chunk_scores = np.zeros(n_chunks, dtype=np.float64)
        for c in range(n_chunks):
            start = c * self.tokens_per_chunk
            end = min(start + self.tokens_per_chunk, seq_len)
            if start < seq_len:
                chunk_scores[c] = head_avg[start:end].sum()

        return chunk_scores


# =============================================================================
# Attention Extraction Engine
# =============================================================================

class RealTraceCollector:
    """Drives autoregressive generation and captures attention distributions."""

    def __init__(self, model, tokenizer, config: CollectionConfig):
        self.model = model
        self.tokenizer = tokenizer
        self.config = config
        self.chunk_mapper: Optional[ChunkMapper] = None

        # Jaccard tracking for online validation
        self._prev_hot_chunks: Optional[set] = None
        self._jaccard_values: List[float] = []

    def _prepare_input(self) -> "torch.Tensor":
        """Prepare the prefill prompt for autoregressive decoding.

        Uses a representative long-context input that exercises diverse
        attention patterns (mixed factual + reasoning content).
        """
        import torch

        if self.config.prompt_file and Path(self.config.prompt_file).exists():
            text = Path(self.config.prompt_file).read_text(encoding="utf-8")
        else:
            # Default: generate a structured prompt that elicits varied attention
            text = self._default_prompt()

        input_ids = self.tokenizer.encode(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=self.config.warmup_tokens,
        )
        # Move to model's device
        device = next(self.model.parameters()).device
        return input_ids.to(device)

    def _default_prompt(self) -> str:
        """Generate a multi-paragraph prompt for diverse attention patterns."""
        rng = np.random.default_rng(self.config.seed)
        # Mix of structured data + narrative to stress different attention heads
        paragraphs = [
            "The following document contains a detailed technical analysis of "
            "memory hierarchy design for large language model inference systems. "
            "Key considerations include CXL memory bandwidth utilization, KV cache "
            "placement strategies, and attention-aware prefetching policies.",
            "",
            "Section 1: Memory Architecture Overview",
            "Modern LLM inference workloads exhibit highly skewed memory access "
            "patterns. The key-value cache grows linearly with sequence length, "
            "creating bandwidth pressure on the memory subsystem. CXL.mem provides "
            "a viable expansion tier with 32-64 GB/s bandwidth and sub-200ns latency.",
            "",
            "Section 2: Attention Distribution Analysis",
            "Empirical measurements on production workloads show that attention mass "
            "concentrates on a small subset of cached tokens. The top 4% of KV cache "
            "entries typically account for over 80% of total attention weight. This "
            "skewness motivates selective prefetching strategies.",
            "",
            "Section 3: Temporal Correlation",
            "Consecutive decode steps exhibit Jaccard similarity of 0.60-0.70 in their "
            "hot token sets. This temporal locality enables predictive admission: chunks "
            "that were important in step t are likely important in step t+1.",
            "",
        ]
        # Pad with semi-random structured content to reach warmup length
        facts = [
            f"Entry {i}: bandwidth={rng.integers(4, 64)} GB/s, "
            f"latency={rng.integers(80, 400)} ns, "
            f"utilization={rng.uniform(0.3, 0.95):.2f}"
            for i in range(200)
        ]
        return "\n".join(paragraphs + facts)

    def collect(self) -> List[Dict]:
        """Run autoregressive decode and collect attention traces.

        Returns list of per-step records in gen_causal_trace.py format.
        """
        import torch

        input_ids = self._prepare_input()
        seq_len = input_ids.shape[1]
        print(f"\n  Prefill tokens: {seq_len}")
        print(f"  Decode steps to collect: {self.config.num_steps}")

        # Initialize chunk mapper from model config
        self.chunk_mapper = ChunkMapper.from_model_config(
            self.model.config,
            seq_len + self.config.num_steps,
            self.config.chunk_size_bytes,
        )
        print(f"  Tokens per chunk: {self.chunk_mapper.tokens_per_chunk}")
        print(f"  Chunks per layer: {self.chunk_mapper.num_chunks_per_layer}")
        print(f"  Total layers: {self.chunk_mapper.num_layers}")

        # Store config for downstream
        self.config.tokens_per_chunk = self.chunk_mapper.tokens_per_chunk
        self.config.total_chunks = self.chunk_mapper.num_chunks_per_layer

        records = []
        past_key_values = None

        # Head-to-tenant assignment: distribute attention heads across tenants
        # Qwen2.5-7B has 28 attention heads → round-robin to 16 tenants
        num_heads = self.model.config.num_attention_heads
        head_to_tenant = [h % self.config.num_tenants for h in range(num_heads)]

        print(f"\n  Collecting attention traces...")
        print(f"  {'Step':<8}{'Seq Len':<10}{'Top Chunk':<12}"
              f"{'Jaccard':<10}{'VRAM (GB)':<10}")
        print(f"  {'-'*48}")

        with torch.no_grad():
            for step in range(self.config.num_steps):
                current_seq_len = seq_len + step

                # Forward pass with attention output
                outputs = self.model(
                    input_ids=input_ids,
                    past_key_values=past_key_values,
                    use_cache=True,
                    output_attentions=True,
                    return_dict=True,
                )

                # Extract attention weights: tuple of [batch, heads, q_len, kv_len]
                attentions = outputs.attentions  # tuple of length num_layers
                past_key_values = outputs.past_key_values

                # Process attention for this decode step
                step_records = self._process_step_attention(
                    attentions, step, current_seq_len, head_to_tenant
                )
                records.extend(step_records)

                # Sample next token (greedy for reproducibility)
                next_token_logits = outputs.logits[:, -1, :]
                next_token = torch.argmax(next_token_logits, dim=-1, keepdim=True)
                input_ids = next_token  # next step only feeds the new token

                # Progress reporting
                if step % 50 == 0 or step == self.config.num_steps - 1:
                    vram = (torch.cuda.memory_allocated() / 1e9
                            if torch.cuda.is_available() else 0.0)
                    jaccard_str = (f"{self._jaccard_values[-1]:.3f}"
                                   if self._jaccard_values else "N/A")
                    # Find the most attended chunk this step
                    top_chunk = "—"
                    if step_records:
                        all_descs = [d for r in step_records for d in r["descriptors"]]
                        if all_descs:
                            top_chunk = str(max(all_descs, key=lambda d: d["priority"])["chunk_id"])
                    print(f"  {step:<8}{current_seq_len:<10}{top_chunk:<12}"
                          f"{jaccard_str:<10}{vram:<10.2f}")

                # Periodic GC to avoid fragmentation
                if step % 100 == 0:
                    gc.collect()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()

        return records

    def _process_step_attention(
        self,
        attentions: tuple,
        step: int,
        seq_len: int,
        head_to_tenant: List[int],
    ) -> List[Dict]:
        """Convert raw attention weights to per-tenant descriptor records.

        For each layer, aggregates attention mass into physical chunks.
        Then assigns chunks to tenants based on which attention heads
        contributed the most mass (round-robin head→tenant mapping).

        Returns records in gen_causal_trace.py format.
        """
        import torch

        num_layers = len(attentions)
        n_chunks = self.chunk_mapper.num_chunks_per_layer

        # Accumulate per-tenant chunk scores across all layers
        # tenant_scores[tid] = [n_chunks] aggregate attention mass
        tenant_scores: Dict[int, np.ndarray] = {
            tid: np.zeros(n_chunks, dtype=np.float64)
            for tid in range(self.config.num_tenants)
        }

        for layer_idx in range(num_layers):
            # attentions[layer] shape: [batch=1, num_heads, q_len=1, kv_len]
            attn_layer = attentions[layer_idx][0, :, -1, :].float().cpu().numpy()
            # attn_layer shape: [num_heads, kv_len]
            num_heads_layer = attn_layer.shape[0]

            for head in range(num_heads_layer):
                tid = head_to_tenant[head % len(head_to_tenant)]
                # Aggregate this head's attention into chunk scores
                head_attn = attn_layer[head]  # [kv_len]
                for c in range(n_chunks):
                    start = c * self.chunk_mapper.tokens_per_chunk
                    end = min(start + self.chunk_mapper.tokens_per_chunk, len(head_attn))
                    if start < len(head_attn):
                        tenant_scores[tid][c] += head_attn[start:end].sum()

        # Generate per-tenant records
        records = []
        step_hot_chunks_all = set()

        for tid in range(self.config.num_tenants):
            scores = tenant_scores[tid]

            # Normalize scores to [0, 255] priority range
            max_score = scores.max()
            if max_score > 0:
                norm_scores = scores / max_score
            else:
                norm_scores = scores

            # Select top-K chunks as descriptors
            k = min(self.config.k_budget, n_chunks)
            top_k_indices = np.argsort(norm_scores)[::-1][:k]

            descriptors = []
            for chunk_id in top_k_indices:
                priority = int(np.clip(norm_scores[chunk_id] * 255, 1, 255))
                descriptors.append({
                    "chunk_id": int(chunk_id),
                    "priority": priority,
                    "epoch": step,
                })
                step_hot_chunks_all.add(int(chunk_id))

            records.append({
                "step": step,
                "tenant_id": tid,
                "descriptors": descriptors,
            })

        # Update Jaccard tracking (across all tenants combined)
        if self._prev_hot_chunks is not None:
            union = self._prev_hot_chunks | step_hot_chunks_all
            intersection = self._prev_hot_chunks & step_hot_chunks_all
            if len(union) > 0:
                jaccard = len(intersection) / len(union)
                self._jaccard_values.append(jaccard)
        self._prev_hot_chunks = step_hot_chunks_all

        return records

    def validate_jaccard(self) -> float:
        """Validate that measured Jaccard aligns with paper's assumptions.

        Asserts 0.55 < mean_jaccard < 0.75 (paper claims ~0.65 for real LLMs).
        """
        if not self._jaccard_values:
            raise RuntimeError("No Jaccard measurements collected. "
                               "Run collect() first.")

        mean_j = float(np.mean(self._jaccard_values))
        std_j = float(np.std(self._jaccard_values))
        p5 = float(np.percentile(self._jaccard_values, 5))
        p95 = float(np.percentile(self._jaccard_values, 95))

        print(f"\n  Jaccard Self-Correlation Statistics:")
        print(f"    Mean:   {mean_j:.4f}")
        print(f"    Std:    {std_j:.4f}")
        print(f"    P5:     {p5:.4f}")
        print(f"    P95:    {p95:.4f}")
        print(f"    Target: 0.55 < mean < 0.75 (paper: ~0.65)")

        if not (0.55 < mean_j < 0.75):
            raise AssertionError(
                f"Jaccard self-correlation ({mean_j:.4f}) outside expected "
                f"range [0.55, 0.75]. This may indicate:\n"
                f"  - Model configuration issue (check attn_implementation)\n"
                f"  - Prompt too short for stable attention patterns\n"
                f"  - Sequence length insufficient for chunk-level correlation\n"
                f"Consider increasing --warmup-tokens or using a longer prompt."
            )

        print(f"    PASSED: {mean_j:.4f} ∈ (0.55, 0.75)")
        return mean_j


# =============================================================================
# Serialization: JSONL + NPZ output
# =============================================================================

def write_jsonl(records: List[Dict], path: str) -> None:
    """Write records in gen_causal_trace.py-compatible JSONL format."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    print(f"\n  JSONL written: {output} ({len(records)} records)")


def write_npz(records: List[Dict], config: CollectionConfig, path: str,
              jaccard_values: List[float]) -> None:
    """Write trace in NPZ format for programmatic consumption.

    Keys (compatible with downstream experiments):
        - steps:          [N_records] step index
        - tenant_ids:     [N_records] tenant index
        - chunk_ids:      [N_records, K] chunk IDs per step per tenant
        - priorities:     [N_records, K] priority scores (0-255)
        - epochs:         [N_records, K] epoch stamps
        - jaccard_trace:  [N_steps-1] per-step Jaccard values
        - config:         serialized configuration dict

    Where N_records = num_steps × num_tenants, K = k_budget.
    """
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)

    n_records = len(records)
    k = config.k_budget

    steps = np.zeros(n_records, dtype=np.int32)
    tenant_ids = np.zeros(n_records, dtype=np.int32)
    chunk_ids = np.zeros((n_records, k), dtype=np.int32)
    priorities = np.zeros((n_records, k), dtype=np.uint8)
    epochs = np.zeros((n_records, k), dtype=np.int32)

    for i, record in enumerate(records):
        steps[i] = record["step"]
        tenant_ids[i] = record["tenant_id"]
        descs = record["descriptors"]
        for j, d in enumerate(descs[:k]):
            chunk_ids[i, j] = d["chunk_id"]
            priorities[i, j] = d["priority"]
            epochs[i, j] = d["epoch"]

    np.savez_compressed(
        output,
        steps=steps,
        tenant_ids=tenant_ids,
        chunk_ids=chunk_ids,
        priorities=priorities,
        epochs=epochs,
        jaccard_trace=np.array(jaccard_values, dtype=np.float32),
        tokens_per_chunk=np.array([config.tokens_per_chunk], dtype=np.int32),
        total_chunks=np.array([config.total_chunks], dtype=np.int32),
        k_budget=np.array([config.k_budget], dtype=np.int32),
        num_tenants=np.array([config.num_tenants], dtype=np.int32),
        num_steps=np.array([config.num_steps], dtype=np.int32),
    )
    print(f"  NPZ written: {output} ({output.stat().st_size / 1e6:.1f} MB)")


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Collect real LLM attention traces for PROSE-APEX validation"
    )
    parser.add_argument("--model", type=str, default="Qwen/Qwen2.5-7B",
                        help="HuggingFace model ID (default: Qwen/Qwen2.5-7B)")
    parser.add_argument("--steps", type=int, default=2000,
                        help="Number of decode steps to collect (default: 2000)")
    parser.add_argument("--k-budget", type=int, default=25,
                        help="Top-K chunks per step (default: 25)")
    parser.add_argument("--chunk-size", type=int, default=65536,
                        help="Chunk size in bytes (default: 65536 = 64KB)")
    parser.add_argument("--tenants", type=int, default=16,
                        help="Number of simulated tenants (default: 16)")
    parser.add_argument("--warmup-tokens", type=int, default=512,
                        help="Prefill context length (default: 512)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--dtype", type=str, default="bfloat16",
                        choices=["bfloat16", "float16", "float32"],
                        help="Model precision (default: bfloat16)")
    parser.add_argument("--allow-cpu-offload", action="store_true",
                        help="Allow CPU/disk offload for low-VRAM setups")
    parser.add_argument("--output", type=str,
                        default="experiments/out/data/trace_real.jsonl",
                        help="Output JSONL path")
    parser.add_argument("--npz-out", type=str, default=None,
                        help="Also save as .npz (optional)")
    parser.add_argument("--validate", action="store_true",
                        help="Run Jaccard validation assertion after collection")
    parser.add_argument("--prompt-file", type=str, default=None,
                        help="Path to custom prompt text file")

    args = parser.parse_args()

    config = CollectionConfig(
        model_id=args.model,
        num_steps=args.steps,
        k_budget=args.k_budget,
        chunk_size_bytes=args.chunk_size,
        num_tenants=args.tenants,
        warmup_tokens=args.warmup_tokens,
        seed=args.seed,
        dtype=args.dtype,
        allow_cpu_offload=args.allow_cpu_offload,
        output_path=args.output,
        npz_path=args.npz_out,
        validate=args.validate,
        prompt_file=args.prompt_file,
    )

    print("=" * 70)
    print("PROSE-APEX Real LLM Attention Trace Collector")
    print("=" * 70)
    print(f"  Model:          {config.model_id}")
    print(f"  Decode steps:   {config.num_steps}")
    print(f"  K budget:       {config.k_budget}")
    print(f"  Chunk size:     {config.chunk_size_bytes // 1024} KB")
    print(f"  Tenants:        {config.num_tenants}")
    print(f"  Warmup tokens:  {config.warmup_tokens}")
    print(f"  Precision:      {config.dtype}")
    print(f"  Seed:           {config.seed}")
    print(f"  Output:         {config.output_path}")
    if config.npz_path:
        print(f"  NPZ output:     {config.npz_path}")
    print("=" * 70)

    # Pre-flight checks
    print("\n[1/4] VRAM pre-check...")
    check_vram_availability(config)

    # Load model
    print("\n[2/4] Loading model...")
    model, tokenizer = load_model(config)

    # Collect traces
    print("\n[3/4] Collecting attention traces...")
    collector = RealTraceCollector(model, tokenizer, config)
    records = collector.collect()

    # Validate Jaccard
    if config.validate or True:  # Always report, assert only if --validate
        try:
            mean_jaccard = collector.validate_jaccard()
        except AssertionError as e:
            if config.validate:
                print(f"\n  VALIDATION FAILED: {e}")
                sys.exit(1)
            else:
                print(f"\n  WARNING (non-fatal): {e}")
                mean_jaccard = float(np.mean(collector._jaccard_values))

    # Serialize
    print("\n[4/4] Writing output...")
    write_jsonl(records, config.output_path)

    if config.npz_path:
        write_npz(records, config, config.npz_path, collector._jaccard_values)

    # Summary
    n_descriptors = sum(len(r["descriptors"]) for r in records)
    print(f"\n{'='*70}")
    print(f"  Collection complete.")
    print(f"  Records:      {len(records)}")
    print(f"  Descriptors:  {n_descriptors}")
    print(f"  Mean Jaccard: {mean_jaccard:.4f}")
    print(f"  Output:       {config.output_path}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()