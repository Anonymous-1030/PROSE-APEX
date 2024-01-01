# PROSE-APEX Host Software Stack

Production-grade host-side driver and inference scheduling software for the
PROSE-APEX CXL Type-3 hardware endpoint. Complements the RTL admission
pipeline (`rtl/`) and the calibrated SimCXL simulator (`simcxl_ext/`).

## Architecture Overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│                        Host CPU  (x86-64)                            │
│                                                                      │
│  ┌────────────────┐  ┌───────────────────┐  ┌────────────────────┐  │
│  │ ProseAllocator │  │   ProseRuntime    │  │ Feedback Collector │  │
│  │ GPU HBM + CXL  │  │  BDB pack + MMIO │  │ attn mass → CXL   │  │
│  └───────┬────────┘  └────────┬──────────┘  └─────────┬──────────┘  │
│          │                     │                        │             │
│          ▼                     ▼                        ▼             │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │             MMIO BAR  (Write-Combining, WC)                   │   │
│  │  [ BDB Header ][ Desc 0 ] … [ Desc N ][ Doorbell @ 0xFFC ]   │   │
│  └───────────────────────────────┬───────────────────────────────┘   │
└──────────────────────────────────┼───────────────────────────────────┘
                                   │  CXL.mem x16 (64 GT/s, ~55 GB/s)
┌──────────────────────────────────┼───────────────────────────────────┐
│                     PROSE-APEX  CXL Type-3 Endpoint                  │
│                                                                      │
│  ┌────────┐  ┌──────┐  ┌──────────────┐  ┌────────┐  ┌──────────┐  │
│  │ Queue  │─▶│ PCM  │─▶│ Expert Bank  │─▶│ Top-K  │─▶│ DMA /    │  │
│  │Dequeue │  │2-cyc │  │ + MAC Score  │  │ Heap   │  │ Compl.   │  │
│  └────────┘  └──┬───┘  └──────────────┘  └────────┘  └──────────┘  │
│                 │ REJECT (4 cycles)                  ADMIT (9 cycles)│
│                 ▼                                                     │
│         Null completion (no DMA)    ◀── RPE = 0 Guarantee            │
└──────────────────────────────────────────────────────────────────────┘
                                   │  DMA → GPU HBM
┌──────────────────────────────────┼───────────────────────────────────┐
│                         GPU  (NVIDIA, sm_80+)                        │
│                                                                      │
│  ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────────┐ │
│  │ mark_visible    │  │ FlashAttention-2 │  │ feedback_writeback  │ │
│  │ __threadfence   │  │ + mass accumulate│  │   PTX st.global.cs  │ │
│  │ + atomicOr      │  │ (per-chunk sum)  │  │   → CXL.mem async   │ │
│  └─────────────────┘  └──────────────────┘  └─────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

## File Structure

```text
host_sw/
├── prose_types.h           Core structures: BDB_Header, ProseDescriptor (64B),
│                           ProseCplStatus, LockFreeRing<T> (SPSC)
├── prose_allocator.h       ProseAllocator class interface
├── prose_allocator.cpp     Dual-tier slab allocator (cudaMalloc / CXL mmap)
├── prose_runtime.h         ProseRuntime class interface
├── prose_runtime.cpp       BDB build, streaming MMIO write, doorbell, poll
├── prose_gpu_bridge.cu     mark_visible_kernel, PVM validation state machine
├── prose_feedback.cu       Attention mass injection + CXL.mem writeback
├── main.cpp                End-to-end integration test (544 descriptors)
├── cxl_devmem.h            Portable CXL Type-3 backend: real devdax / NUMA node,
│                           self-labelled emulation fallback
├── bench_modeb_e2e.cpp     Single-host Mode B (pull) E2E benchmark on real CXL
├── run_modeb_hw.sh         Driver: runs the bench + captures HW provenance
├── CMakeLists.txt          CMake build (PROSE_SIM=ON/OFF)
└── README.md               This file
```

## Build & Run

### Simulation Mode (no hardware required)

```bash
cd host_sw
# Direct compilation (quick)
g++ -std=c++17 -DPROSE_SIM -O2 -o prose_apex_test main.cpp prose_allocator.cpp prose_runtime.cpp
./prose_apex_test

# Or via CMake
mkdir build && cd build
cmake .. -DPROSE_SIM=ON
make -j$(nproc)
./prose_apex_test
```

### Production Mode (requires CUDA Toolkit + CXL hardware)

```bash
mkdir build && cd build
cmake .. -DPROSE_SIM=OFF -DCMAKE_CUDA_ARCHITECTURES="80;86;89;90"
make -j$(nproc)
sudo ./prose_apex_test    # Needs /dev/dax0.0 access for CXL mmap
```

## Runtime staleness instrumentation

`RuntimeTraceSink` records monotonic JSONL events for allocator residency
transitions, descriptor generation/submission, endpoint ingress/dequeue, DMA
commit, and completion observation. Pass
the same optional sink to `ProseAllocator` and `ProseRuntime`; leaving it null
keeps the original hot path and API behavior.

```cpp
auto trace = std::make_shared<prose::RuntimeTraceSink>("runtime_trace.jsonl");
auto allocator = std::make_shared<prose::ProseAllocator>(
    hbm_bytes, cxl_bytes, prose::kDefaultChunkSize, trace);
prose::ProseRuntime runtime(mmio_base, allocator, vc_id, pasid, trace);
```

Without multiple CXL hosts, the companion experiment uses real OS processes,
the repository KV descriptor stream, a concurrent allocator, and an endpoint
queue to capture the same event schema before replaying measured stale verdicts
in SimCXL:

```bash
python experiments/run_runtime_staleness.py --repeats 5
```

Outputs are written under `experiments/out/runtime_staleness/`, including the
raw 16-process JSONL stream, summary JSON, and vector PDF figure. Provenance in
the summary explicitly labels this as process-emulated hosts, not physical CXL.

The two measured intervals have different meanings. The long
generation-to-dequeue interval is the window in which a descriptor can become
stale. The 250 ns dequeue-to-commit interval is the endpoint's final atomic
validation window; it is a protocol parameter, not a claim that the entire race
lasts only 250 ns. In the host runtime, cancellation ends when the BDB is
submitted and its doorbell is posted. A descriptor may still be in transport
before endpoint ingress, but the host can no longer retract that posted batch.

## Single-host Mode B on a real CXL Type-3 device

`bench_modeb_e2e` answers the "it's all simulation" objection with an
end-to-end run of the **Mode B (endpoint-gated pull)** protocol against a
*commodity* CXL Type-3 memory device — no custom endpoint silicon, no
endpoint-side DMA engine.

**Why no custom hardware is needed.** A Type-3 device is Host-managed Device
Memory (HDM): the OS exposes it as a `devdax` character device or a CPU-less
CXL NUMA node, and the payload region is plain memory reached over CXL.mem
loads/stores. In Mode B the device never pushes payload — it only hands the host
a reservation *token* (a decision). The **host** issues the payload transfer,
and only for chunks it holds a valid token for:

```text
reject  →  no token  →  host issues no load  →  no payload on the link  →  RPE = 0
```

That ordering — *decide before pull* — is the whole single-host RPE=0 argument,
and it is substrate-independent. (The endpoint's extra value across trust
domains — CFO, no cross-host race — is a separate, multi-host claim.)

**Falsifiability.** An RPE=0 number is meaningless unless the instrument can see
a leak. The benchmark runs a fetch-then-score (FTS) control through the *same*
byte counter on the *same* device: FTS reads every candidate before scoring, so
its rejected reads show up as RPE > 0. The run only passes if Mode B reports
RPE == 0 **and** FTS reports RPE > 0.

```bash
cd host_sw
# Real device (single host): point at your Type-3 devdax node.
PROSE_CXL_DEVDAX=/dev/dax0.0 ./run_modeb_hw.sh --steps 500
# or explicitly:
./run_modeb_hw.sh --devdax /dev/dax0.0 --steps 500

# No CXL device present: still runs the protocol, self-labelled EMULATED.
./run_modeb_hw.sh --steps 500
```

The driver writes `experiments/out/modeb_hw/`:
`modeb_result.json` (RPE + promotion-latency percentiles), `modeb_run.log`, and
`hw_provenance.txt` — a snapshot of `cxl list` / `daxctl list` / `numactl -H` /
CXL `dmesg` lines so a reviewer can confirm the numbers came from silicon. The
JSON's `real_cxl` field is `true` only on a genuine CXL substrate; an emulated
run can never masquerade as hardware.

## Integration Test Output

```text
=======================================================
  PROSE-APEX Host Software Stack — Integration Test
=======================================================

[1/6] Initializing ProseAllocator...
  HBM pool: 1024 MB available
  CXL pool: 1024 MB available
[2/6] Initializing ProseRuntime...
  VC ID: 0, Current epoch: 42
[3/6] Generating 512 chunk requests...
  Pre-resident chunks: 8 (will trigger PCM REJECT)
  Total requests: 544 (512 unique + 32 duplicates)
  Stale-epoch requests: ~10 (every 47th)
[4/6] Submitting batch via BDB + Doorbell...
  Submitted: 544 descriptors in 24.3 µs
  BDBs generated: 9 (at 64 descriptors/BDB max)
[5/6] Simulating PCM + polling completions...
  ADMITTED:      493 (90.6%)
  REJECTED_PCM:  51 (9.4%)
  REJECTED_HEAP: 0
[6/6] Computing statistics...
  RPE (Reclaimed-Payload Exposure): 0 ✓ ZERO
  All assertions passed.
```

## Key Design Decisions

| Mechanism | Implementation | Rationale |
| --------- | -------------- | --------- |
| MMIO writes | `_mm_stream_si128` + `_mm_sfence` | Bypass CPU cache; write-combining to CXL BAR without polluting L1/L2 |
| Submission queue | Lock-free SPSC ring (release/acquire) | CXL 3.1 §8.2.4 MMIO ordering; single-producer avoids lock contention |
| Doorbell | Single UC 32-bit store to `mmio_base+0xFFC` | Endpoint latches on write strobe; must follow sfence |
| GPU visibility | `__threadfence_system()` → `atomicOr` | System-level fence ensures DMA from CXL agent visible to GPU SM |
| Feedback writeback | PTX `st.global.cs.u64` | Cache-streaming bypasses L2; non-blocking on compute SMs |
| Backpressure | Spin with exponential backoff (100 ms timeout) | Prevents MMIO ring overflow; graceful degradation under load |
| RPE guarantee | PCM rejects *before* DMA issue | Rejected descriptors never trigger payload transfer — zero exposure |

## Correspondence to RTL

| Software concept | Hardware module (`rtl/`) | Notes |
| ---------------- | ------------------------ | ----- |
| `submit_batch()` → BDB | `APEX_PIPELINE` S1 dequeue | MMIO ring feeds the descriptor dequeue stage |
| `ProseCplStatus::REJECTED_PCM` | `APEX_PCM` | 2-cycle epoch/namespace/residency check |
| Priority field in `ProseDescriptor` | `APEX_MAC_ARRAY` score | Expert-weighted priority for Top-K admission |
| `ADMITTED` → DMA | `APEX_TOPK_HEAP` admit → S7 DMA issue | 9-cycle admit path |
| `feedback_writeback_kernel` | Weight-update input | Closes the online-learning loop in `APEX_WEIGHT_UPDATE` |

## Integration with vLLM / TGI

The software stack exposes C-linkage APIs for framework integration:

```cpp
// GPU bridge — mark chunks visible after DMA (prose_gpu_bridge.cu)
extern "C" uint32_t prose_mark_visible_batch(
    uint32_t* valid_bitmap, const uint8_t* chunk_data,
    uint32_t chunk_size, const uint32_t* chunk_ids,
    const uint32_t* expected_checksums, uint32_t num_chunks,
    uint32_t* fallback_flags, cudaStream_t stream);

// Feedback — async writeback of attention mass (prose_feedback.cu)
extern "C" void prose_launch_feedback_writeback(
    uint64_t* cxl_mem_ptr, const float* mass_buffer,
    const uint32_t* chunk_ids, uint32_t num_chunks,
    cudaStream_t stream);
```

These can be called from Python via `ctypes` / `cffi`, or integrated into
vLLM's attention backend as a custom `PagedAttention` extension that replaces
the KV-cache eviction policy with PROSE-APEX hardware-accelerated admission.

## License

See top-level [LICENSE](../LICENSE).
