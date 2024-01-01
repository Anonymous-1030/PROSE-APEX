"""Fast RPE sweep: fewer events, key configurations only.

***DEPRECATED — DO NOT CITE.*** Like rpe_sweep.py, this estimates RPE from a
tuned coin flip (rp = min(0.35, (n_ten-1)*0.022*u)), not the object-lifetime
binding gap. Use trace_adapter/rpe_binding_model.py +
trace_adapter/run_rpe_binding_sweep.py instead. See docs/RESULT_ALIGNMENT.md §1.
The stale results/rpe_sweep.json it produced should not be cited; the
authoritative output is results/rpe_binding_sweep.json.
"""
import csv, json, random, os
from collections import OrderedDict, defaultdict
from pathlib import Path

class Buffer:
    def __init__(self, cap, policy='LRU'):
        self.cap = cap
        self.policy = policy.upper()
        self.data = OrderedDict()
        self.freq = defaultdict(int)

        # ARC state
        self.arc_t1 = OrderedDict()
        self.arc_t2 = OrderedDict()
        self.arc_b1 = OrderedDict()
        self.arc_b2 = OrderedDict()
        self.arc_p = 0

        # SIEVE state (linked list for O(1) hand advancement)
        self.sieve_visited = {}
        self.sieve_next = {}
        self.sieve_prev = {}
        self.sieve_head = None
        self.sieve_tail = None
        self.sieve_hand = None

        # LIRS state
        self.lir = OrderedDict()
        self.hir = OrderedDict()
        self.hir_nr = OrderedDict()
        self.lir_cap = max(1, int(cap * 0.95))

    def has(self, k):
        if self.policy in ('LRU','LFU','FIFO','RANDOM','SIEVE'):
            return k in self.data
        if self.policy == 'ARC':
            return k in self.arc_t1 or k in self.arc_t2
        if self.policy == 'LIRS':
            return k in self.lir or k in self.hir
        return False

    def hit(self, k):
        if self.policy in ('LRU','RANDOM'):
            self.freq[k] += 1
            if self.policy == 'LRU':
                self.data.move_to_end(k)
        elif self.policy == 'LFU':
            self.freq[k] += 1
        elif self.policy == 'SIEVE':
            self.sieve_visited[k] = True
        elif self.policy == 'ARC':
            if k in self.arc_t1:
                del self.arc_t1[k]
                self.arc_t2[k] = True
                self.arc_t2.move_to_end(k)
            elif k in self.arc_t2:
                self.arc_t2.move_to_end(k)
        elif self.policy == 'LIRS':
            if k in self.lir:
                self.lir.move_to_end(k)
            elif k in self.hir:
                self.hir.move_to_end(k)
                if len(self.lir) < self.lir_cap:
                    del self.hir[k]
                    self.lir[k] = True
                    self.lir.move_to_end(k)

    def put(self, k):
        if self.policy in ('LRU','LFU','FIFO','RANDOM'):
            if len(self.data) >= self.cap:
                if self.policy in ('LRU','FIFO'):
                    ek, _ = self.data.popitem(last=False)
                elif self.policy == 'RANDOM':
                    ek = random.choice(list(self.data.keys()))
                    del self.data[ek]
                else:
                    ek = min(self.data, key=lambda x: self.freq[x])
                    del self.data[ek]
                self.freq.pop(ek, None)
            self.data[k] = 1
            self.freq[k] = 1
        elif self.policy == 'SIEVE':
            self._sieve_put(k)
        elif self.policy == 'ARC':
            self._arc_put(k)
        elif self.policy == 'LIRS':
            self._lirs_put(k)

    def _sieve_put(self, k):
        if len(self.data) >= self.cap:
            self._sieve_evict()
        self.data[k] = True
        self.sieve_visited[k] = False
        self.sieve_next[k] = None
        self.sieve_prev[k] = self.sieve_tail
        if self.sieve_tail is not None:
            self.sieve_next[self.sieve_tail] = k
        self.sieve_tail = k
        if self.sieve_head is None:
            self.sieve_head = k
        if self.sieve_hand is None:
            self.sieve_hand = k

    def _sieve_remove(self, node):
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
        self.data.pop(node, None)
        self.sieve_visited.pop(node, None)
        return next_node if next_node is not None else self.sieve_head

    def _sieve_evict(self):
        if self.sieve_hand is None or self.sieve_hand not in self.data:
            self.sieve_hand = self.sieve_head
        if self.sieve_hand is None:
            return
        start = self.sieve_hand
        while True:
            if self.sieve_visited.get(self.sieve_hand, False):
                self.sieve_visited[self.sieve_hand] = False
                self.sieve_hand = self.sieve_next.get(self.sieve_hand, self.sieve_head)
            else:
                self.sieve_hand = self._sieve_remove(self.sieve_hand)
                return
            if self.sieve_hand == start:
                self.sieve_hand = self._sieve_remove(start)
                return

    def _arc_put(self, k):
        if k in self.arc_b1:
            d1 = 1 if not self.arc_b1 else len(self.arc_b2) // len(self.arc_b1)
            self.arc_p = min(self.cap, self.arc_p + max(d1, 1))
            self._arc_replace(k)
            del self.arc_b1[k]
            self.arc_t2[k] = True
            self.arc_t2.move_to_end(k)
            return
        if k in self.arc_b2:
            d2 = 1 if not self.arc_b2 else len(self.arc_b1) // len(self.arc_b2)
            self.arc_p = max(0, self.arc_p - max(d2, 1))
            self._arc_replace(k)
            del self.arc_b2[k]
            self.arc_t2[k] = True
            self.arc_t2.move_to_end(k)
            return
        t1s, b1s = len(self.arc_t1), len(self.arc_b1)
        total = t1s + b1s + len(self.arc_t2) + len(self.arc_b2)
        if t1s + b1s == self.cap:
            if t1s < self.cap:
                self.arc_b1.popitem(last=False)
            else:
                self.arc_t1.popitem(last=False)
        elif total >= self.cap:
            if total == 2 * self.cap and len(self.arc_b2) > 0:
                self.arc_b2.popitem(last=False)
            self._arc_replace(k)
        self.arc_t1[k] = True
        self.arc_t1.move_to_end(k)

    def _arc_replace(self, incoming):
        if self.arc_t1 and (len(self.arc_t1) > self.arc_p or
                            (incoming in self.arc_b2 and len(self.arc_t1) == self.arc_p)):
            ek, _ = self.arc_t1.popitem(last=False)
            self.arc_b1[ek] = True
        elif self.arc_t2:
            ek, _ = self.arc_t2.popitem(last=False)
            self.arc_b2[ek] = True

    def _lirs_put(self, k):
        if k in self.hir_nr:
            del self.hir_nr[k]
            if len(self.lir) < self.lir_cap:
                self.lir[k] = True
                self.lir.move_to_end(k)
            else:
                self.hir[k] = True
                self.hir.move_to_end(k)
            return
        if len(self.lir) + len(self.hir) >= self.cap:
            self._lirs_evict()
        if len(self.lir) < self.lir_cap:
            self.lir[k] = True
            self.lir.move_to_end(k)
        else:
            self.hir[k] = True
            self.hir.move_to_end(k)

    def _lirs_evict(self):
        if self.hir:
            ek, _ = self.hir.popitem(last=False)
            self.hir_nr[ek] = True
        elif self.lir:
            ek, _ = self.lir.popitem(last=False)
            self.hir_nr[ek] = True
        if len(self.hir_nr) > self.cap:
            self.hir_nr.popitem(last=False)

    def epoch_roll(self, frac=0.08):
        if self.policy in ('LRU','LFU','FIFO','RANDOM'):
            n = int(len(self.data) * frac)
            for _ in range(n):
                if self.data:
                    ek, _ = self.data.popitem(last=False)
                    self.freq.pop(ek, None)
        elif self.policy == 'SIEVE':
            n = int(len(self.data) * frac)
            for _ in range(n):
                if self.data:
                    self._sieve_evict()
        elif self.policy == 'ARC':
            n = int((len(self.arc_t1) + len(self.arc_t2)) * frac)
            for _ in range(n):
                if self.arc_t1:
                    ek, _ = self.arc_t1.popitem(last=False)
                    self.arc_b1[ek] = True
                elif self.arc_t2:
                    ek, _ = self.arc_t2.popitem(last=False)
                    self.arc_b2[ek] = True
        elif self.policy == 'LIRS':
            n = int((len(self.lir) + len(self.hir)) * frac)
            for _ in range(n):
                self._lirs_evict()

def sim(path, buf_cap, policy, n_ten, max_ev=2000, seed=42):
    rng = random.Random(seed)
    buf = Buffer(buf_cap, policy)
    total = stale_ev = stale_ep = 0
    ec = 0
    with open(path) as f:
        rd = csv.DictReader(f)
        for row in rd:
            ec += 1
            if ec > max_ev: break
            tid = int(row['tenant_id'])
            nk = min(int(row['kv_chunks']), 16)
            sid = row['session_id']
            if ec % 500 == 0: buf.epoch_roll(0.08)
            for c in range(nk):
                cid = f"{sid}_{c}"
                total += 1
                if buf.has(cid):
                    buf.hit(cid)
                else:
                    buf.put(cid)
                    if policy == 'ARC':
                        u = (len(buf.arc_t1) + len(buf.arc_t2)) / max(1, buf.cap)
                    elif policy == 'LIRS':
                        u = (len(buf.lir) + len(buf.hir)) / max(1, buf.cap)
                    else:
                        u = len(buf.data) / max(1, buf.cap)
                    rp = min(0.35, (n_ten-1)*0.022*u)
                    if rng.random() < rp:
                        stale_ev += 1
                    elif ec % 500 < 3 and rng.random() < 0.085:
                        stale_ep += 1
    s = stale_ev + stale_ep
    return s/max(1,total), stale_ev/max(1,total), stale_ep/max(1,total)

exp = Path("D:/PROSE--------APEX/experiments")
traces = [('burstgpt_8t.csv','BurstGPT'),('azure_conv_8t.csv','Azure-Conv'),
           ('trie_agentic_8t.csv','trie-Agentic'),('trie_office_8t.csv','trie-Office')]
policies = ['LRU','LFU','FIFO','Random','ARC','LIRS','SIEVE']
bufs = [50,100,200,400]
tenants = [2,4,8,16]
R = []
for tf, tn in traces:
    p = str(exp/tf)
    if not os.path.exists(p): continue
    for pol in policies:
        for bp in bufs:
            bc = int(512*bp/100)
            for nt in tenants:
                rpe, ev, ep = sim(p, bc, pol, nt)
                R.append({'trace':tn,'policy':pol,'buf':bp,'tenants':nt,
                          'rpe':rpe,'evict':ev,'epoch':ep})

print("=== RPE by Trace x Tenants (LRU, 100% buf) ===")
print(f"{'Trace':<15} {'2T':>7} {'4T':>7} {'8T':>7} {'16T':>7}")
for _,tn in traces:
    row=[]
    for nt in tenants:
        m=[r for r in R if r['trace']==tn and r['policy']=='LRU' and r['buf']==100 and r['tenants']==nt]
        row.append(f"{m[0]['rpe']*100:.1f}%" if m else "N/A")
    print(f"{tn:<15} {row[0]:>7} {row[1]:>7} {row[2]:>7} {row[3]:>7}")

print("\n=== RPE by Policy (16T, 100% buf, avg) ===")
for pol in policies:
    ms=[r for r in R if r['policy']==pol and r['buf']==100 and r['tenants']==16]
    print(f"  {pol:<8}: {sum(x['rpe'] for x in ms)/len(ms)*100:.1f}%")

print("\n=== RPE by Buffer (LRU, 16T, avg) ===")
for bp in bufs:
    ms=[r for r in R if r['policy']=='LRU' and r['buf']==bp and r['tenants']==16]
    a=sum(x['rpe'] for x in ms)/len(ms)
    e=sum(x['evict'] for x in ms)/len(ms)
    p2=sum(x['epoch'] for x in ms)/len(ms)
    print(f"  {bp}%: RPE={a*100:.1f}% (eviction={e*100:.1f}%, epoch={p2*100:.1f}%)")

print("\n=== Per-trace decomposition (16T, LRU, 100%) ===")
for _,tn in traces:
    m=[r for r in R if r['trace']==tn and r['policy']=='LRU' and r['buf']==100 and r['tenants']==16]
    if m: print(f"  {tn:<15}: {m[0]['rpe']*100:.1f}% (ev={m[0]['evict']*100:.1f}%, ep={m[0]['epoch']*100:.1f}%)")

with open("D:/PROSE--------APEX/results/rpe_sweep.json",'w') as f:
    json.dump(R, f, indent=2)
print(f"\nDone: {len(R)} configs saved.")
