"""
RPE Sensitivity Sweep  ***DEPRECATED — DO NOT CITE***

This script estimated RPE from a TUNED probability
    race_prob = min(0.35, (num_tenants - 1) * 0.02 * buffer_util)
which is a heuristic, not a measurement of the object-lifetime binding gap. Its
output does not correspond to the paper's Definition 1 and does not match the
paper's Table III.

The authoritative, mechanistic model is:
    trace_adapter/rpe_binding_model.py   (snapshot-vs-issue-time binding)
    trace_adapter/run_rpe_binding_sweep.py
See docs/RESULT_ALIGNMENT.md §1 for why this file was retired and how the honest
model reconciles with the paper (11–14% at heavy oversubscription, ~2% at 1×).

Retained only for historical diff of the eviction-policy implementations.

--- original docstring ---
Simulates endpoint buffer dynamics under varying:
  - Eviction policies: LRU, LFU, FIFO, Random, ARC, LIRS, SIEVE
  - Buffer sizes: 50%, 100%, 200%, 400% of working set
  - Tenant counts: 2, 4, 8, 16
  - Epoch rollover intervals

Measures stale-admit rate (RPE) per configuration on each trace.
"""

import csv
import json
import random
import os
from collections import OrderedDict, defaultdict
from pathlib import Path

CHUNK_GRANULARITY = 64


class EndpointBuffer:
    """Simulates CXL endpoint buffer with eviction policies."""

    def __init__(self, capacity: int, policy: str = 'LRU'):
        self.capacity = capacity
        self.policy = policy.upper()
        self.contents = OrderedDict()  # chunk_id -> (tenant_id, insert_time, freq)
        self.freq_map = defaultdict(int)
        self.time_counter = 0
        self.eviction_count = 0

        # ARC state
        self.arc_t1 = OrderedDict()  # recent cache
        self.arc_t2 = OrderedDict()  # frequent cache
        self.arc_b1 = OrderedDict()  # ghost for t1
        self.arc_b2 = OrderedDict()  # ghost for t2
        self.arc_p = 0               # target size for t1

        # SIEVE state (linked list for O(1) hand advancement)
        self.sieve_visited = {}
        self.sieve_next: Dict[str, Optional[str]] = {}
        self.sieve_prev: Dict[str, Optional[str]] = {}
        self.sieve_head: Optional[str] = None
        self.sieve_tail: Optional[str] = None
        self.sieve_hand: Optional[str] = None

        # LIRS state
        self.lirs_lir = OrderedDict()    # low inter-reference recency (resident)
        self.lirs_hir = OrderedDict()    # high inter-reference recency (resident)
        self.lirs_hir_non_resident = OrderedDict()  # non-resident HIR history
        self.lirs_lirs_capacity = max(1, int(capacity * 0.95))

    def contains(self, chunk_id: str) -> bool:
        if self.policy in ('LRU', 'LFU', 'FIFO', 'RANDOM'):
            return chunk_id in self.contents
        if self.policy == 'ARC':
            return chunk_id in self.arc_t1 or chunk_id in self.arc_t2
        if self.policy == 'SIEVE':
            return chunk_id in self.contents
        if self.policy == 'LIRS':
            return chunk_id in self.lirs_lir or chunk_id in self.lirs_hir
        return False

    def access(self, chunk_id: str, tenant_id: int) -> bool:
        """Returns True if chunk was already resident (hit)."""
        self.time_counter += 1
        if self.policy == 'LRU':
            if chunk_id in self.contents:
                self.freq_map[chunk_id] += 1
                self.contents.move_to_end(chunk_id)
                return True
            return False
        if self.policy == 'LFU':
            if chunk_id in self.contents:
                self.freq_map[chunk_id] += 1
                return True
            return False
        if self.policy == 'FIFO':
            return chunk_id in self.contents
        if self.policy == 'RANDOM':
            if chunk_id in self.contents:
                self.freq_map[chunk_id] += 1
                return True
            return False
        if self.policy == 'ARC':
            return self._arc_access(chunk_id)
        if self.policy == 'SIEVE':
            if chunk_id in self.contents:
                self.sieve_visited[chunk_id] = True
                return True
            return False
        if self.policy == 'LIRS':
            return self._lirs_access(chunk_id)
        return False

    def insert(self, chunk_id: str, tenant_id: int):
        """Insert chunk, evicting if needed. Returns evicted chunk_id or None."""
        self.time_counter += 1
        if self.policy in ('LRU', 'LFU', 'FIFO', 'RANDOM'):
            evicted = None
            if len(self.contents) >= self.capacity:
                evicted = self._evict()
            self.contents[chunk_id] = (tenant_id, self.time_counter, 1)
            self.freq_map[chunk_id] = 1
            return evicted
        if self.policy == 'ARC':
            return self._arc_insert(chunk_id)
        if self.policy == 'SIEVE':
            return self._sieve_insert(chunk_id)
        if self.policy == 'LIRS':
            return self._lirs_insert(chunk_id)
        return None

    def _evict(self) -> str:
        self.eviction_count += 1
        if self.policy == 'LRU':
            chunk_id, _ = self.contents.popitem(last=False)
        elif self.policy == 'FIFO':
            chunk_id, _ = self.contents.popitem(last=False)
        elif self.policy == 'LFU':
            min_freq = min(self.freq_map[k] for k in self.contents)
            for k in self.contents:
                if self.freq_map[k] == min_freq:
                    chunk_id = k
                    break
            del self.contents[chunk_id]
        elif self.policy == 'RANDOM':
            chunk_id = random.choice(list(self.contents.keys()))
            del self.contents[chunk_id]
        else:
            chunk_id, _ = self.contents.popitem(last=False)
        if chunk_id in self.freq_map:
            del self.freq_map[chunk_id]
        return chunk_id

    # ------------------------------------------------------------------
    # ARC (Adaptive Replacement Cache)
    # ------------------------------------------------------------------
    def _arc_access(self, chunk_id: str) -> bool:
        if chunk_id in self.arc_t1:
            # Promote to T2 (frequent)
            del self.arc_t1[chunk_id]
            self.arc_t2[chunk_id] = True
            self.arc_t2.move_to_end(chunk_id)
            return True
        if chunk_id in self.arc_t2:
            self.arc_t2.move_to_end(chunk_id)
            return True
        return False

    def _arc_insert(self, chunk_id: str):
        # Case 1: chunk in B1 (ghost of T1)
        if chunk_id in self.arc_b1:
            delta1 = 1 if len(self.arc_b1) == 0 else len(self.arc_b2) // len(self.arc_b1)
            self.arc_p = min(self.capacity, self.arc_p + max(delta1, 1))
            self._arc_replace(chunk_id)
            del self.arc_b1[chunk_id]
            self.arc_t2[chunk_id] = True
            self.arc_t2.move_to_end(chunk_id)
            return None

        # Case 2: chunk in B2 (ghost of T2)
        if chunk_id in self.arc_b2:
            delta2 = 1 if len(self.arc_b2) == 0 else len(self.arc_b1) // len(self.arc_b2)
            self.arc_p = max(0, self.arc_p - max(delta2, 1))
            self._arc_replace(chunk_id)
            del self.arc_b2[chunk_id]
            self.arc_t2[chunk_id] = True
            self.arc_t2.move_to_end(chunk_id)
            return None

        # Case 3: complete miss
        t1_size = len(self.arc_t1)
        b1_size = len(self.arc_b1)
        total_ghost = t1_size + b1_size + len(self.arc_t2) + len(self.arc_b2)

        evicted = None
        if t1_size + b1_size == self.capacity:
            if t1_size < self.capacity:
                # Evict LRU from B1
                self.arc_b1.popitem(last=False)
            else:
                # T1 is full and B1 empty, evict LRU from T1
                evicted_key, _ = self.arc_t1.popitem(last=False)
                evicted = evicted_key
        elif total_ghost >= self.capacity:
            if total_ghost == 2 * self.capacity and len(self.arc_b2) > 0:
                self.arc_b2.popitem(last=False)
            self._arc_replace(chunk_id)

        self.arc_t1[chunk_id] = True
        self.arc_t1.move_to_end(chunk_id)
        return evicted

    def _arc_replace(self, incoming_key: str):
        """Move one block from T1 or T2 to its ghost list to make room."""
        t1_size = len(self.arc_t1)
        if t1_size > 0 and (t1_size > self.arc_p or
                            (incoming_key in self.arc_b2 and t1_size == self.arc_p)):
            evicted, _ = self.arc_t1.popitem(last=False)
            self.arc_b1[evicted] = True
        elif len(self.arc_t2) > 0:
            evicted, _ = self.arc_t2.popitem(last=False)
            self.arc_b2[evicted] = True

    # ------------------------------------------------------------------
    # SIEVE
    # ------------------------------------------------------------------
    def _sieve_insert(self, chunk_id: str):
        evicted = None
        if len(self.contents) >= self.capacity:
            evicted = self._sieve_evict()
        self.contents[chunk_id] = True
        self.sieve_visited[chunk_id] = False
        # Append to tail of linked list
        self.sieve_next[chunk_id] = None
        self.sieve_prev[chunk_id] = self.sieve_tail
        if self.sieve_tail is not None:
            self.sieve_next[self.sieve_tail] = chunk_id
        self.sieve_tail = chunk_id
        if self.sieve_head is None:
            self.sieve_head = chunk_id
        if self.sieve_hand is None:
            self.sieve_hand = chunk_id
        return evicted

    def _sieve_remove_node(self, node: str) -> Optional[str]:
        """Remove node from linked list and contents; return next node (or head)."""
        prev_node = self.sieve_prev.pop(node, None)
        next_node = self.sieve_next.pop(node, None)
        if prev_node is not None:
            self.sieve_next[prev_node] = next_node
        else:
            self.sieve_head = next_node
        if next_node is not None:
            self.sieve_prev[next_node] = prev_node
        else:
            self.sieve_tail = prev_node
        self.contents.pop(node, None)
        self.sieve_visited.pop(node, None)
        return next_node if next_node is not None else self.sieve_head

    def _sieve_evict(self) -> Optional[str]:
        if self.sieve_hand is None or self.sieve_hand not in self.contents:
            self.sieve_hand = self.sieve_head
        if self.sieve_hand is None:
            return None

        start_hand = self.sieve_hand
        while True:
            hand = self.sieve_hand
            if self.sieve_visited.get(hand, False):
                self.sieve_visited[hand] = False
                self.sieve_hand = self.sieve_next.get(hand, self.sieve_head)
            else:
                self.sieve_hand = self._sieve_remove_node(hand)
                return hand
            if self.sieve_hand == start_hand:
                # All visited; evict start_hand
                self.sieve_hand = self._sieve_remove_node(start_hand)
                return start_hand

    # ------------------------------------------------------------------
    # LIRS (simplified faithful implementation)
    # ------------------------------------------------------------------
    def _lirs_access(self, chunk_id: str) -> bool:
        if chunk_id in self.lirs_lir:
            self.lirs_lir.move_to_end(chunk_id)
            return True
        if chunk_id in self.lirs_hir:
            self.lirs_hir.move_to_end(chunk_id)
            # A HIR block accessed again is promoted to LIR if space permits
            if len(self.lirs_lir) < self.lirs_lirs_capacity:
                del self.lirs_hir[chunk_id]
                self.lirs_lir[chunk_id] = True
                self.lirs_lir.move_to_end(chunk_id)
            return True
        return False

    def _lirs_insert(self, chunk_id: str):
        evicted = None
        if chunk_id in self.lirs_hir_non_resident:
            # Recirculated HIR: treat as a reuse, promote if possible
            del self.lirs_hir_non_resident[chunk_id]
            if len(self.lirs_lir) < self.lirs_lirs_capacity:
                self.lirs_lir[chunk_id] = True
                self.lirs_lir.move_to_end(chunk_id)
            else:
                self.lirs_hir[chunk_id] = True
                self.lirs_hir.move_to_end(chunk_id)
            return evicted

        total_resident = len(self.lirs_lir) + len(self.lirs_hir)
        if total_resident >= self.capacity:
            evicted = self._lirs_evict()

        if len(self.lirs_lir) < self.lirs_lirs_capacity:
            self.lirs_lir[chunk_id] = True
            self.lirs_lir.move_to_end(chunk_id)
        else:
            self.lirs_hir[chunk_id] = True
            self.lirs_hir.move_to_end(chunk_id)
        return evicted

    def _lirs_evict(self) -> str:
        # Evict the oldest HIR resident; if none, demote oldest LIR to non-resident HIR
        if self.lirs_hir:
            evicted, _ = self.lirs_hir.popitem(last=False)
            self.lirs_hir_non_resident[evicted] = True
            if len(self.lirs_hir_non_resident) > self.capacity:
                self.lirs_hir_non_resident.popitem(last=False)
            return evicted
        if self.lirs_lir:
            evicted, _ = self.lirs_lir.popitem(last=False)
            self.lirs_hir_non_resident[evicted] = True
            if len(self.lirs_hir_non_resident) > self.capacity:
                self.lirs_hir_non_resident.popitem(last=False)
            return evicted
        return None

    def epoch_rollover(self, fraction: float = 0.1):
        """Simulate epoch rollover: invalidate fraction of buffer."""
        if self.policy in ('LRU', 'LFU', 'FIFO', 'RANDOM'):
            to_remove = int(len(self.contents) * fraction)
            removed = []
            for _ in range(to_remove):
                if self.contents:
                    chunk_id, _ = self.contents.popitem(last=False)
                    if chunk_id in self.freq_map:
                        del self.freq_map[chunk_id]
                    removed.append(chunk_id)
            return removed
        if self.policy == 'ARC':
            # Approximate: clear a fraction from T1 and T2, ghosts stay
            to_remove = int((len(self.arc_t1) + len(self.arc_t2)) * fraction)
            removed = []
            for _ in range(to_remove):
                if self.arc_t1:
                    k, _ = self.arc_t1.popitem(last=False)
                    self.arc_b1[k] = True
                    removed.append(k)
                elif self.arc_t2:
                    k, _ = self.arc_t2.popitem(last=False)
                    self.arc_b2[k] = True
                    removed.append(k)
            return removed
        if self.policy == 'SIEVE':
            to_remove = int(len(self.contents) * fraction)
            removed = []
            for _ in range(to_remove):
                if self.contents:
                    k = self._sieve_evict()
                    if k is not None:
                        removed.append(k)
            return removed
        if self.policy == 'LIRS':
            to_remove = int((len(self.lirs_lir) + len(self.lirs_hir)) * fraction)
            removed = []
            for _ in range(to_remove):
                k = self._lirs_evict()
                if k is not None:
                    removed.append(k)
            return removed
        return []


def simulate_rpe(trace_path: str, buffer_capacity: int, policy: str,
                 num_tenants: int, epoch_interval: int = 500,
                 max_events: int = 100000, seed: int = 42) -> dict:
    """
    Simulate RPE for a trace configuration.
    Host maintains shadow state; endpoint maintains true state.
    RPE = descriptors host thinks valid but endpoint has evicted.
    """
    rng = random.Random(seed)
    buffer = EndpointBuffer(buffer_capacity, policy)

    # Host shadow state per tenant
    host_shadow = defaultdict(set)  # tenant_id -> set of chunk_ids host thinks are resident

    total_descriptors = 0
    stale_admits = 0
    eviction_race_stale = 0
    epoch_race_stale = 0
    event_count = 0
    epoch_counter = 0

    with open(trace_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            event_count += 1
            if event_count > max_events:
                break

            tenant_id = int(row['tenant_id'])
            kv_chunks = int(row['kv_chunks'])
            session_id = row['session_id']

            # Generate chunk descriptors for this request
            chunk_ids = [f"{session_id}_c{c}" for c in range(kv_chunks)]

            # Simulate epoch rollover periodically
            epoch_counter += 1
            if epoch_counter % epoch_interval == 0:
                evicted_by_epoch = buffer.epoch_rollover(fraction=0.08)
                # Host does NOT see this
                for eid in evicted_by_epoch:
                    pass  # host shadow remains stale

            # Host decides to promote chunks it thinks are not yet resident
            for cid in chunk_ids[:min(32, len(chunk_ids))]:  # budget 32 per step
                total_descriptors += 1

                # Host checks shadow: thinks this needs promotion
                # Check endpoint truth
                if buffer.contains(cid):
                    # Already there, no RPE (host could have known)
                    buffer.access(cid, tenant_id)
                    host_shadow[tenant_id].add(cid)
                else:
                    # Host issues descriptor. But was it in shadow?
                    was_in_shadow = cid in host_shadow[tenant_id]

                    # Insert into endpoint buffer (may evict something)
                    evicted = buffer.insert(cid, tenant_id)

                    # If eviction happened, other tenants' shadows become stale
                    if evicted:
                        for tid in host_shadow:
                            if evicted in host_shadow[tid]:
                                host_shadow[tid].discard(evicted)
                                # But there's a race window!

                    host_shadow[tenant_id].add(cid)

                    # Simulate the race: between host descriptor issue and
                    # endpoint dequeue, another tenant may have caused eviction
                    # Model: with N tenants, probability of race = f(N, buffer_util)
                    if buffer.policy == 'ARC':
                        buffer_util = (len(buffer.arc_t1) + len(buffer.arc_t2)) / max(1, buffer.capacity)
                    elif buffer.policy == 'LIRS':
                        buffer_util = (len(buffer.lirs_lir) + len(buffer.lirs_hir)) / max(1, buffer.capacity)
                    else:
                        buffer_util = len(buffer.contents) / max(1, buffer.capacity)
                    race_prob = min(0.35, (num_tenants - 1) * 0.02 * buffer_util)

                    if rng.random() < race_prob:
                        stale_admits += 1
                        eviction_race_stale += 1
                    elif epoch_counter % epoch_interval < 3 and rng.random() < 0.08:
                        stale_admits += 1
                        epoch_race_stale += 1

    rpe_rate = stale_admits / max(1, total_descriptors)
    return {
        'trace': os.path.basename(trace_path),
        'policy': policy,
        'buffer_pct': int(100 * buffer_capacity / max(1, buffer_capacity)),
        'buffer_capacity': buffer_capacity,
        'num_tenants': num_tenants,
        'epoch_interval': epoch_interval,
        'total_descriptors': total_descriptors,
        'stale_admits': stale_admits,
        'rpe_rate': rpe_rate,
        'eviction_race_pct': eviction_race_stale / max(1, total_descriptors),
        'epoch_race_pct': epoch_race_stale / max(1, total_descriptors),
        'buffer_evictions': buffer.eviction_count,
    }


def run_sweep():
    exp_dir = Path("D:/PROSE--------APEX/experiments")
    results_dir = Path("D:/PROSE--------APEX/results")
    results_dir.mkdir(exist_ok=True)

    traces = [
        ('burstgpt_8t.csv', 'BurstGPT'),
        ('azure_conv_8t.csv', 'Azure-Conv'),
        ('trie_agentic_8t.csv', 'trie-Agentic'),
        ('trie_office_8t.csv', 'trie-Office'),
    ]

    policies = ['LRU', 'ARC', 'LIRS', 'SIEVE']
    buffer_pcts = [50, 100, 200, 400]  # % of 512-chunk working set
    tenant_counts = [2, 4, 8, 16]

    base_working_set = 512  # chunks

    all_results = []

    for trace_file, trace_name in traces:
        trace_path = str(exp_dir / trace_file)
        if not os.path.exists(trace_path):
            print(f"  Skipping {trace_name}: file not found")
            continue

        for policy in policies:
            for bpct in buffer_pcts:
                buf_cap = int(base_working_set * bpct / 100)
                for nt in tenant_counts:
                    result = simulate_rpe(
                        trace_path, buf_cap, policy, nt,
                        epoch_interval=500,
                        max_events=5000,
                    )
                    result['trace_name'] = trace_name
                    result['buffer_pct'] = bpct
                    all_results.append(result)
                    if nt == 16 and policy == 'LRU' and bpct == 100:
                        print(f"  {trace_name}/{policy}/buf{bpct}%/{nt}T: RPE={result['rpe_rate']:.3f} "
                              f"(evict={result['eviction_race_pct']:.3f}, epoch={result['epoch_race_pct']:.3f})")

    # Write results
    out_path = str(results_dir / "rpe_sensitivity_sweep.csv")
    with open(out_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=all_results[0].keys())
        writer.writeheader()
        writer.writerows(all_results)
    print(f"\nResults written to {out_path} ({len(all_results)} configs)")

    # Summary table for paper
    print("\n=== RPE by Trace x Tenants (LRU, 100% buffer) ===")
    print(f"{'Trace':<15} {'2T':>6} {'4T':>6} {'8T':>6} {'16T':>6}")
    for trace_file, trace_name in traces:
        row = []
        for nt in tenant_counts:
            match = [r for r in all_results
                     if r['trace_name'] == trace_name and r['policy'] == 'LRU'
                     and r['buffer_pct'] == 100 and r['num_tenants'] == nt]
            if match:
                row.append(f"{match[0]['rpe_rate']*100:.1f}%")
            else:
                row.append("N/A")
        print(f"{trace_name:<15} {row[0]:>6} {row[1]:>6} {row[2]:>6} {row[3]:>6}")

    # Summary by eviction policy (paper Table III style)
    print("\n=== RPE by Policy (16 tenants, 100% buffer, all traces averaged) ===")
    for policy in policies:
        matches = [r for r in all_results
                   if r['policy'] == policy and r['buffer_pct'] == 100
                   and r['num_tenants'] == 16]
        if matches:
            avg = sum(r['rpe_rate'] for r in matches) / len(matches)
            print(f"  {policy:<8}: {avg*100:.1f}%")

    return all_results


if __name__ == '__main__':
    print("Running RPE Sensitivity Sweep...")
    run_sweep()
