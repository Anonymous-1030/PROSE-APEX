# Result Alignment: paper tables vs. released artifact

This note states, for each headline quantitative claim, exactly how the released
artifact reproduces it, where the artifact uses a different input or model than
the paper text, and which numbers are measured, modeled, or projected. It is the
honest bridge between the paper's definitions and what the code actually computes.

The guiding rule for this release: **no number is tuned to hit a paper value.**
Where a model produces something different from the paper, we report the model's
output and explain the gap, rather than fitting a constant.

---

## 1. Table III — RPE on public traces (§IV-B)

**Paper:** BurstGPT (1.43M req) 12.8%, Splitwise Conv (19K) 14.4%, trie (8K) 11.2%
unmitigated RPEpayload at 16 hosts / LRU / 1× buffer; endpoint gate → 0 on all.

### 1a. Trace substitution: "Splitwise Conv" → "Azure-Conv"

The released traces are `burstgpt_8t.csv`, `azure_conv_8t.csv`,
`trie_agentic_8t.csv`, `trie_office_8t.csv`. The repo does **not** ship a
Splitwise conversation trace; `azure_conv_8t.csv` (Azure LLM inference
conversational trace) stands in for the conversational-workload row. The paper
text and Table III should either (a) rename that row "Azure-Conv", or (b) add the
Splitwise trace to the artifact. **Until one of those is done, Table III's middle
row is Azure-Conv, not Splitwise.** This is a naming/provenance gap, flagged
openly here.

### 1b. RPE model: heuristic coin-flip → mechanistic binding model

The earlier sweep scripts (`trace_adapter/rpe_fast.py`,
`trace_adapter/rpe_sweep.py`) computed RPE from a **tuned probability**
`rp = min(0.35, (tenants-1)·k·utilisation)`. That is not a measurement of the
object-lifetime binding gap, and its output (`results/rpe_sweep.json`:
BurstGPT 32%, Azure-Conv 33%, trie <2% at 16h/LRU/1×) matches neither the paper
nor physical intuition. Those files are retained only for historical diff; **do
not cite them.**

The authoritative model is `trace_adapter/rpe_binding_model.py`, driven by
`trace_adapter/run_rpe_binding_sweep.py`. It implements the paper's Definition 1
directly:

* each physical frame carries a generation that bumps on every reuse;
* a promotion descriptor records `(frame, chunk, generation)` at *snapshot* time;
* the descriptor issues after a per-descriptor queue residence drawn from an
  exponential distribution (VC-contention proxy), during which other tenants'
  admissions reuse frames;
* **RPE occurs iff the descriptor's `(frame, chunk, generation)` binding changed
  before it issued** — the wrong-object condition of Definition 1.

Nothing is fitted: RPE emerges from each trace's own reuse structure and the
queue-residence-vs-reuse-horizon overlap.

### 1c. What the honest model reports

`results/rpe_binding_summary.txt` (16 tenants, mean queue residence 64
pool-admit ticks):

| Trace        | buf 50% (≈2× oversub) | buf 100% (1×) |
|--------------|-----------------------|---------------|
| BurstGPT     | 13.7%                 | 1.9%          |
| Azure-Conv   | 13.6%                 | 1.9%          |
| trie-Agentic | 15.1%                 | 2.0%          |
| trie-Office  | 14.9%                 | 1.9%          |

**Reconciliation with the paper.** The paper's 11.2–14.4% band is reproduced at
**heavy oversubscription** (buf 50%), which is the regime the paper's headline
targets ("once the tenant count reaches 8", "heavily oversubscribed"). At a 1×
buffer the same model gives ~2%, consistent with the repo's own rebuttal note
(`run_trace_sensitivity.py`: nominal ≈ 2–3%). The value is stable across trace
lengths (13.5–13.7% at 4k/8k/16k events), so it is not a truncation artifact.

**Recommended paper edit:** state that the 11.2–14.4% figures are at the
oversubscribed operating point (buffer ≈ 0.5× working set), and report the ~2%
nominal (1×) figure alongside, rather than presenting the high band as the 1×
result. The gated result (RPEpayload = 0) holds by construction across all
configurations and is asserted, not sampled.

---

## 2. Table V — Same-contract Recovery@K (§IV-E)

**Paper:** APEX-Core2 0.918/0.948/0.921/0.879 across shift regimes, etc.

**Artifact status:** these exact per-regime values are **not emitted by any
shipped script**. `experiments/run_s2_robustness.py` and
`scripts/quest_cxl_baseline.py` produce Recovery@K on a *synthetic* attention
model (no real LLM is loaded — see §4). The scorer ranking, the Quest-CXL
collapse to the random floor, and the InfiniGen-CXL causal margin are
reproducible in *shape*, but the specific decimals in Table V are not pinned to a
released run. **Do not present Table V as reproduced from the artifact** until a
driver emits exactly those numbers with a fixed seed; today it is a modeled
illustration of the ranking gap, not a measured table.

---

## 3. Table VI — Budget vs. task accuracy (§IV-F)

**Paper:** normalized task accuracy 0.812 (Qwen2.5-7B) at 50% budget, etc., and
Recovery@K 0.904 at 50%.

**Artifact status:** `experiments/run_budget_accuracy.py` computes Recovery@K
from a synthetic useful-chunk model (`USEFUL_FRACTION = 0.04`,
`semantic_strength = 0.80`), **not** from LLM task evaluation. The monotonic
budget→accuracy curve is reproducible as a model; the absolute per-model task
accuracies (Qwen/LLaMA/Mistral) are **not** produced by running those models.

---

## 4. Measured vs. modeled vs. projected — the evidence classes

| Claim family | Class in artifact | Notes |
|--------------|-------------------|-------|
| Mode B reject-before-pull, RPE=0 | **Measured** (commodity Type-3 or self-labelled emulation) | `host_sw/bench_modeb_e2e.cpp`; `real_cxl` flag true only on real devdax |
| RTL admit/reject cycle counts (8/4 internal, 9/4 with MMIO) | **RTL-derived** | `make sim`, `run_rtl_xcheck.py` |
| ASAP7 area/timing of scoring pipeline (0.024 mm², MAC +43 ps) | **Liberty-estimated** | `asic/synth_estimate.py`; full-endpoint 1.069 mm²/78 mW is a **projection**, not in `asic/reports/` |
| Mode A throughput 3.1×/5.9× | **Projected** (SimCXL) | no synthesizable DMA engine ships; see LIMITATIONS |
| RPE percentages (Table III) | **Modeled** (binding model, this note) | trace-driven, not silicon |
| Recovery@K / task accuracy (Tables V, VI) | **Modeled** (synthetic attention) | no LLM inference in the artifact |
| SimCXL projection numbers (P99, saturation) | **Modeled**; base SimCXL/Cohet simulator is **external** and not vendored | only `simcxl_ext` ships |

**No real LLM runs in this artifact.** `scripts/collect_real_trace.py` can
collect real traces if `torch`/`transformers` and a GPU are available, but the
released results do not depend on it. Recovery@K and task accuracy are computed
from a statistical attention model, and should be described that way.

---

## 5. Reproduced-as-stated (these do match)

| Quantity | Source | Matches paper |
|----------|--------|---------------|
| Protection-span ratios: REFCNT 2.96×, 2PHASE 2.78×, PROSE 1.00× | `results/baselines/summary_aggregate.csv` | yes (Fig 3) |
| Segmented-64 control/header overhead 37.6% | same | yes (§IV-C) |
| REFCNT / 2PHASE / PROSE exact-zero stale payload | same | yes |
| Quest-CXL Recovery@K ≈ random floor over CXL | `scripts/quest_cxl_baseline.py` | yes (§IV-E) |
| Multi-host P99 18.7 ns (1H) → 290.7 ns (8H) | `run_simcxl_multihost.py` | yes (§IV-D) |
