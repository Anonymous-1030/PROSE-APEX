"""
Unified Trace Adapter for PROSE-APEX Evaluation
Converts BurstGPT, Azure LLM Inference, and trie workloads into a common
event-stream format suitable for SimCXL RPE/CFO/throughput simulation.

Output schema per event:
  timestamp_ms, tenant_id, prompt_tokens, generation_tokens, kv_chunks, session_id
"""

import csv
import json
import os
import random
import hashlib
from pathlib import Path
from typing import Generator

# KV chunk granularity: 64 tokens per chunk (matches paper)
CHUNK_GRANULARITY = 64

def tokens_to_chunks(tokens: int) -> int:
    return max(1, (tokens + CHUNK_GRANULARITY - 1) // CHUNK_GRANULARITY)


# ─── BurstGPT Adapter ───────────────────────────────────────────────────────

def adapt_burstgpt(csv_path: str, num_tenants: int = 8,
                   seed: int = 42) -> Generator[dict, None, None]:
    """
    BurstGPT: 1.43M requests over ~61 days, 2 models (ChatGPT, GPT-4).
    Assign tenants by hashing (timestamp_bin, model) for reproducibility.
    """
    rng = random.Random(seed)
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = float(row['Timestamp'])
            model = row['Model'].strip()
            req_tok = int(row['Request tokens'])
            resp_tok = int(row['Response tokens'])
            if req_tok == 0:
                continue
            # Deterministic tenant assignment
            h = hashlib.md5(f"{int(ts)//60}_{model}".encode()).hexdigest()
            tenant_id = int(h, 16) % num_tenants
            yield {
                'timestamp_ms': ts * 1000.0,
                'tenant_id': tenant_id,
                'prompt_tokens': req_tok,
                'generation_tokens': resp_tok,
                'kv_chunks': tokens_to_chunks(req_tok),
                'session_id': f"burst_{int(ts)}_{tenant_id}",
                'source': 'BurstGPT',
                'model': model,
            }


# ─── Azure LLM Inference Adapter ────────────────────────────────────────────

def adapt_azure_llm(csv_path: str, num_tenants: int = 8,
                    seed: int = 42) -> Generator[dict, None, None]:
    """
    Azure LLM Inference 2023: Code and Conv traces.
    Schema: TIMESTAMP, ContextTokens, GeneratedTokens
    """
    rng = random.Random(seed)
    from datetime import datetime
    base_ts = None
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            ts_str = row['TIMESTAMP'].strip()
            ctx = int(row['ContextTokens'])
            gen = int(row['GeneratedTokens'])
            if ctx == 0:
                continue
            # Parse timestamp to ms offset
            try:
                dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                ts_ms = dt.timestamp() * 1000.0
            except Exception:
                ts_ms = i * 100.0  # fallback
            if base_ts is None:
                base_ts = ts_ms
            # Tenant by round-robin with jitter
            tenant_id = (i + rng.randint(0, 1)) % num_tenants
            yield {
                'timestamp_ms': ts_ms - base_ts,
                'tenant_id': tenant_id,
                'prompt_tokens': ctx,
                'generation_tokens': gen,
                'kv_chunks': tokens_to_chunks(ctx),
                'session_id': f"azure_{i}_{tenant_id}",
                'source': 'AzureLLM',
            }


# ─── trie Workload Adapter ──────────────────────────────────────────────────

def adapt_trie(jsonl_path: str, num_tenants: int = 8,
               arrival_rate_ms: float = 50.0,
               seed: int = 42) -> Generator[dict, None, None]:
    """
    trie workloads: multi-turn agentic traces.
    Each trace becomes a session; each turn becomes an event.
    Prefix overlap is naturally captured by cumulative context growth.
    """
    rng = random.Random(seed)
    ts_cursor = 0.0
    with open(jsonl_path, 'r') as f:
        for session_idx, line in enumerate(f):
            d = json.loads(line)
            tenant_id = session_idx % num_tenants
            num_turns = d['num_turns']
            resp_lens = d['assistant_response_length']
            tool_lens = d['tool_call_output_length']
            tool_lats = d['tool_call_latency']
            prompt_len = d['input_prompt_length']

            # Initial request
            cumulative = prompt_len
            yield {
                'timestamp_ms': ts_cursor,
                'tenant_id': tenant_id,
                'prompt_tokens': prompt_len,
                'generation_tokens': resp_lens[0] if num_turns > 0 else d['final_assistant_response_length'],
                'kv_chunks': tokens_to_chunks(cumulative),
                'session_id': f"trie_{session_idx}",
                'turn': 0,
                'cumulative_tokens': cumulative,
                'prefix_tokens': prompt_len,
                'source': 'trie',
            }

            # Subsequent turns
            for t in range(num_turns):
                ts_cursor += tool_lats[t] * 1000.0 + rng.uniform(5, 20)
                cumulative += resp_lens[t] + tool_lens[t]
                gen = d['final_assistant_response_length'] if t == num_turns - 1 else resp_lens[t+1] if t+1 < num_turns else 0
                yield {
                    'timestamp_ms': ts_cursor,
                    'tenant_id': tenant_id,
                    'prompt_tokens': cumulative,
                    'generation_tokens': gen,
                    'kv_chunks': tokens_to_chunks(cumulative),
                    'session_id': f"trie_{session_idx}",
                    'turn': t + 1,
                    'cumulative_tokens': cumulative,
                    'prefix_tokens': prompt_len,
                    'source': 'trie',
                }

            # Inter-session arrival
            ts_cursor += rng.expovariate(1.0 / arrival_rate_ms)


# ─── Unified Writer ─────────────────────────────────────────────────────────

def write_unified(events: Generator[dict, None, None], output_path: str,
                  max_events: int = 500000):
    """Write events to CSV for SimCXL ingestion."""
    fieldnames = ['timestamp_ms', 'tenant_id', 'prompt_tokens',
                  'generation_tokens', 'kv_chunks', 'session_id', 'source']
    count = 0
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for ev in events:
            writer.writerow(ev)
            count += 1
            if count >= max_events:
                break
    return count


if __name__ == '__main__':
    base = Path(os.environ.get("TRACE_ANALYSIS_DIR", "./trace_analysis"))
    out = Path(os.environ.get("ADAPTER_OUT_DIR",
                              Path(__file__).resolve().parent.parent / "experiments"))

    print("Adapting BurstGPT...")
    burstgpt_path = base / "BurstGPT/BurstGPT-main/data/BurstGPT_1.csv"
    n = write_unified(adapt_burstgpt(str(burstgpt_path), num_tenants=8),
                      str(out / "burstgpt_8t.csv"), max_events=500000)
    print(f"  -> {n} events written")

    print("Adapting Azure LLM (Code)...")
    azure_code = base / "AzurePublicDataset/AzurePublicDataset-master/data/AzureLLMInferenceTrace_code.csv"
    n = write_unified(adapt_azure_llm(str(azure_code), num_tenants=8),
                      str(out / "azure_code_8t.csv"), max_events=500000)
    print(f"  -> {n} events written")

    print("Adapting Azure LLM (Conv)...")
    azure_conv = base / "AzurePublicDataset/AzurePublicDataset-master/data/AzureLLMInferenceTrace_conv.csv"
    n = write_unified(adapt_azure_llm(str(azure_conv), num_tenants=8),
                      str(out / "azure_conv_8t.csv"), max_events=500000)
    print(f"  -> {n} events written")

    print("Adapting trie (agentic_coding)...")
    trie_agentic = base / "trie/trie-main/workloads/agentic_coding_8k.jsonl"
    n = write_unified(adapt_trie(str(trie_agentic), num_tenants=8),
                      str(out / "trie_agentic_8t.csv"), max_events=500000)
    print(f"  -> {n} events written")

    print("Adapting trie (office_work)...")
    trie_office = base / "trie/trie-main/workloads/office_work_8k.jsonl"
    n = write_unified(adapt_trie(str(trie_office), num_tenants=8),
                      str(out / "trie_office_8t.csv"), max_events=500000)
    print(f"  -> {n} events written")

    print("Done.")
