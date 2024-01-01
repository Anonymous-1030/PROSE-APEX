# SimCXL Extension: Inherited vs. Added Parameters

PROSE-APEX is built on **SimCXL** (Cohet et al., *"A CXL-Driven Coherent
Heterogeneous Computing Framework with Hardware-Calibrated Full-System
Simulation,"* HPCA'26) — a hardware-calibrated full-system CXL simulator
validated against real CXL silicon.

This extension **inherits the calibrated SimCXL core unchanged** for CXL.mem
latency, coherence, and bandwidth contention, and **adds** the layers needed to
study causal endpoint admission: MMIO-ring modelling, per-VC endpoint queues,
copy-engine scheduling, and CXL.mem payload timing.

The table below is the authoritative separation (paper Table I). Every added
value carries its calibration source.

## Parameters

| Component | Parameter | Value | Source |
|-----------|-----------|-------|--------|
| **Inherited from the calibrated SimCXL core** | | | |
| CXL.mem read | latency | 170–350 ns | Calibrated vs. silicon (SimCXL) |
| Coherence / BW | contention model | — | Calibrated vs. silicon (SimCXL) |
| CXL.mem protocol | proto-proc latency | 15 ns | SimCXL `SimCXLTiming` |
| CXL bridge | transit | 50 ns | SimCXL `SimCXLTiming` |
| CXL link | bandwidth | 55 GB/s | SimCXL `SimCXLTiming` |
| Queue depths | req / resp | 48 / 48 | SimCXL `SimCXLTiming` |
| **Added by this work** | | | |
| CEFE admit       | 9 ns (RTL); 8-cycle model | — | RTL synthesis (this work, `../rtl`) |
| CEFE PCM reject  | 4 ns (RTL); 3-cycle model | — | RTL synthesis (this work, `../rtl`) |
| CEFE heap reject | 9 ns (RTL); 8-cycle model | — | RTL synthesis (this work, `../rtl`) |
| Copy engine | bandwidth | 32 GB/s | Samsung CMM-D datasheet |
| P2P write | per chunk | ~200 ns | GPUDirect RDMA bench (H100) |
| MMIO ring | doorbell | 45 ns | Intel CXL dev kit (modeled) |
| VC queues (protocol) | credit entries / VC | 8 | CXL 3.1 §8.2 (modeled) |

> **Two distinct "VC depth" numbers — do not conflate.** The `8 entries/VC`
> above is the *CXL-protocol* virtual-channel credit depth inherited from the
> spec and used by the timing model (link-level flow control). It is **not** the
> CEFE endpoint's per-tenant *staging* queue, which is a design-side buffer sized
> to `QUEUE_DEPTH = 32` in the RTL (`rtl/APEX_PKG.sv`, `rtl/cefe_vc_wrr.sv`). The
> protocol credit depth bounds in-flight link requests; the 32-deep staging queue
> absorbs a batch of descriptors per tenant before arbitration. The paper's
> Table II reports the former; the microarchitecture (README, RTL) reports the
> latter.
>
> **What carries RTL backing vs. what is modeled.** The CEFE admit/reject cycle
> counts are cross-checked against the synthesizable APEX pipeline in `../rtl`
> (9-cycle admit, 4-cycle PCM reject, 9-cycle heap reject at 1 GHz; 8/3/8 cycles
> after the shared MMIO dequeue stage). The MMIO-ring and VC-queue parameters are
> *modeled* — their tails are stressed in the paper's endpoint-stress section,
> not silicon-validated. The reproduction drivers label which numbers rest on
> which.

## Where each value lives in the code

| Value | Module |
|-------|--------|
| Inherited SimCXL timing constants | [`simcxl_ext/simcxl_core.py`](../simcxl_ext/simcxl_core.py) — `SimCXLTiming` |
| Endpoint admit/reject cycle counts | [`simcxl_ext/endpoint_sim.py`](../simcxl_ext/endpoint_sim.py) — `EndpointConfig` |
| Per-VC queues + WRR arbitration | [`simcxl_ext/multi_tenant.py`](../simcxl_ext/multi_tenant.py) |
| BDB submission + per-descriptor admission | [`simcxl_ext/descriptor_batching.py`](../simcxl_ext/descriptor_batching.py) |
| Per-VC endpoint queue model (M/D/1) | [`simcxl_ext/cxl_queue_simulator.py`](../simcxl_ext/cxl_queue_simulator.py) |
| Closed-form ordering / RPE model | [`simcxl_ext/cxl_admission_sim.py`](../simcxl_ext/cxl_admission_sim.py) |

## Relation to the RTL

The `simcxl_ext` timing model and the `../rtl` SystemVerilog are two views of
the same datapath. The paper validates the model against the RTL "to within one
cycle": the RTL closes an admit in 9 cycles, a PCM reject in 4 cycles, and a
heap reject in 9 cycles; setting aside the shared MMIO dequeue stage, these are
8/3/8 cycles internally. The per-descriptor cross-check in
`experiments/run_rtl_xcheck.py` implements this three-way split explicitly.
Run `make sim` in `../rtl` to reproduce the cycle counts directly.
