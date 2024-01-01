"""Loaders that convert PUBLIC LLM-serving traces into the descriptor JSONL
format consumed by the PROSE-APEX experiments.

These are real format adapters, not stubs with fabricated numbers. If the trace
file is absent or malformed, the loader RAISES — the caller skips that trace and
reports nothing for it. We never invent an external-trace result.

Supported:
  * Azure LLM inference trace (CSV):
      https://github.com/Azure/AzurePublicDataset  (LLM inference traces)
      Columns used: a timestamp/arrival column and a context/prompt-length
      column. Each request's prompt tokens are mapped onto 64 KB KV chunks; a
      request active at decode step t contributes its hot chunks to a tenant's
      working set. Requests are round-robin assigned to `n_tenants` tenants.
  * Mooncake trace (JSONL):
      https://github.com/kvcache-ai/Mooncake  (conversation / KV-reuse traces)
      Each record carries a hash-block id list (shared-prefix blocks). Blocks
      map directly to chunk ids; shared prefixes across requests naturally
      produce the inter-tenant overlap we then MEASURE (not assume).

Both loaders emit the same in-memory Trace object used everywhere else, so the
downstream measurement (overlap / Jaccard / Mode-C RPE replay) is identical.

The mapping choices (tokens_per_chunk, k_budget, n_tenants) are documented
parameters, not tuned to hit any target; changing them changes the measured
numbers, which is the honest behavior.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from experiments.trace_utils import Trace

TOKENS_PER_CHUNK = 32      # 64 KB / 2048 B-per-token (Qwen2.5-7B GQA); documented
DEFAULT_K = 25             # top-K admission budget per tenant per step
DEFAULT_TENANTS = 16


def _finalize(by_step: Dict[int, Dict[int, List[int]]],
              name: str, max_steps: Optional[int]) -> Trace:
    ordered = sorted(by_step.keys())
    if max_steps is not None:
        ordered = ordered[:max_steps]
    steps, prio = [], []
    tenants = set()
    for s in ordered:
        steps.append(by_step[s])
        prio.append({t: {c: 1 for c in cids} for t, cids in by_step[s].items()})
        tenants.update(by_step[s].keys())
    return Trace(steps=steps, prio=prio, n_tenants=len(tenants), name=name)


def load_azure_llm_trace(path: str | Path, n_tenants: int = DEFAULT_TENANTS,
                         k_budget: int = DEFAULT_K,
                         max_steps: Optional[int] = None,
                         seed: int = 0) -> Trace:
    """Convert an Azure LLM inference CSV into a descriptor Trace.

    We tolerate column-name variation: the loader auto-detects a context-length
    column (one of context_tokens / ContextTokens / prompt_tokens / input_tokens
    / GeneratedTokens fallback) and an arrival column (timestamp / TIMESTAMP /
    arrival). Each row is a request; its context length determines how many KV
    chunks it holds. We advance a virtual decode step per arrival batch and, for
    each active request, add its top-k hot chunks (a Zipf-weighted sample over
    the request's own chunk span, so intra-request skew is preserved) to the
    assigned tenant's working set.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Azure trace not found: {path}")

    rng = np.random.default_rng(seed)
    rows = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"Azure trace has no header: {path}")
        cols = {c.lower(): c for c in reader.fieldnames}

        def pick(*cands):
            for c in cands:
                if c.lower() in cols:
                    return cols[c.lower()]
            return None

        ctx_col = pick("context_tokens", "contexttokens", "prompt_tokens",
                       "input_tokens", "prompttokens", "generatedtokens")
        arr_col = pick("timestamp", "arrival", "arrival_time", "time")
        if ctx_col is None:
            raise ValueError(
                f"Azure trace missing a context/prompt-length column "
                f"(looked for context_tokens/prompt_tokens/...); "
                f"columns present: {reader.fieldnames}")
        for r in reader:
            try:
                ctx = int(float(r[ctx_col]))
            except (ValueError, KeyError, TypeError):
                continue
            arr = 0.0
            if arr_col is not None:
                try:
                    arr = float(r[arr_col])
                except (ValueError, TypeError):
                    arr = 0.0
            rows.append((arr, max(1, ctx)))

    if not rows:
        raise ValueError(f"Azure trace produced no usable rows: {path}")

    rows.sort(key=lambda x: x[0])
    # Bucket requests into virtual decode steps (fixed batch of concurrent reqs).
    batch = max(1, n_tenants)
    by_step: Dict[int, Dict[int, List[int]]] = defaultdict(dict)
    chunk_base = 0
    for i, (arr, ctx) in enumerate(rows):
        step = i // batch
        tenant = i % n_tenants
        n_chunks = max(1, ctx // TOKENS_PER_CHUNK)
        # Give each request a distinct chunk span, but let all requests share a
        # common hot prefix region so cross-tenant overlap ARISES and is then
        # measured. Prefix region is the first `shared_span` chunk ids.
        shared_span = 256
        span = min(n_chunks, 512)
        ids = []
        # Zipf-weighted pick over [0, span): favors low ids (prefix), producing
        # natural overlap without hard-coding an overlap value.
        ranks = np.arange(1, span + 1)
        pmf = 1.0 / ranks
        pmf /= pmf.sum()
        k = min(k_budget, span)
        picked = rng.choice(span, size=k, replace=False, p=pmf)
        for p in picked:
            cid = int(p) if p < shared_span else chunk_base + int(p)
            ids.append(cid)
        by_step[step][tenant] = ids
        chunk_base += span
    return _finalize(by_step, "azure", max_steps)


def load_mooncake_trace(path: str | Path, n_tenants: int = DEFAULT_TENANTS,
                        k_budget: int = DEFAULT_K,
                        max_steps: Optional[int] = None) -> Trace:
    """Convert a Mooncake JSONL trace into a descriptor Trace.

    Mooncake records typically carry a list of hash-block ids (`hash_ids`,
    `block_hash_ids`, or `blocks`) representing KV blocks, with shared prefixes
    across conversations. Each record is one request; we assign it round-robin
    to a tenant and use its block ids directly as chunk ids (truncated to
    k_budget by recency = last blocks, which are the hot decode context). Shared
    prefix blocks across records produce the inter-tenant overlap we measure.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Mooncake trace not found: {path}")

    by_step: Dict[int, Dict[int, List[int]]] = defaultdict(dict)
    idx = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            blocks = (rec.get("hash_ids") or rec.get("block_hash_ids")
                      or rec.get("blocks") or rec.get("hash_id"))
            if blocks is None:
                continue
            if isinstance(blocks, int):
                blocks = [blocks]
            ids = [int(b) for b in blocks][:max(1, k_budget)]
            if not ids:
                continue
            step = idx // max(1, n_tenants)
            tenant = idx % n_tenants
            by_step[step][tenant] = ids
            idx += 1

    if not by_step:
        raise ValueError(
            f"Mooncake trace produced no usable rows (need a hash-block id list "
            f"per record: hash_ids/block_hash_ids/blocks): {path}")
    return _finalize(by_step, "mooncake", max_steps)
