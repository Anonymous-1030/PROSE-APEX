"""SimCXL extension for PROSE-APEX: causal endpoint admission over CXL.

This package extends the hardware-calibrated SimCXL full-system CXL simulator
(Cohet, HPCA'26) with the model needed to study **PROSE-APEX**: causal endpoint
admission for KV-cache promotion over a non-preemptive CXL payload path.

It inherits SimCXL's calibrated CXL.mem latency, coherence, and bandwidth-
contention core unchanged (``simcxl_core.SimCXLTiming``), and adds the layers
this work introduces:

* **CEFE** — *Causal Endpoint Front-End.* A pre-payload admission gate at the
  CXL endpoint. It binds the accept/reject verdict *before* a descriptor enters
  the non-preemptive CXL.mem payload path, so a descriptor that fails validation
  moves no payload. This eliminates *Reclaimed-Payload Exposure* (RPE): moving
  the bytes of a slot after it has been reused for a different object version.
* **PCM** — *Payload Commitment Mechanism.* Validation-before-visibility: an
  admitted chunk becomes visible to the attention kernel only after its epoch,
  namespace, and integrity checks pass.
* **CFO** — *Coalesced Fan-Out.* One physical read of a declared shared source,
  fanned out to every requesting domain, matched on a session-setup handle.
* **BDB / VC-WRR** — Batch Descriptor Block submission and per-tenant
  virtual-channel weighted-round-robin arbitration for multi-host isolation.

Public modules
--------------
``simcxl_core``         Inherited SimCXL timing / protocol constants
                        (``SimCXLTiming``, ``CXLCmd``).
``endpoint_sim``        Cycle-level endpoint descriptor-burst simulator: admit /
                        reject paths, DMA back-pressure, per-VC queuing.
``cxl_admission_sim``   Closed-form ordering / RPE model (zero-RPE vs
                        fetch-then-score).
``descriptor_batching`` CEFE BDB submission and per-descriptor admission.
``cxl_queue_simulator`` Per-VC endpoint queue modelling (M/D/1).
``multi_tenant``        VC-WRR multi-tenant isolation and CFO accounting.
``io_utils``            JSON / figure helpers for the experiments.

The terminology here is the authoritative one from the paper. See
``docs/SIMCXL_EXTENSION.md`` for the inherited-vs-added parameter table and the
calibration source of every added value.
"""

from .simcxl_core import SimCXLTiming, CXLCmd  # noqa: F401

__all__ = ["SimCXLTiming", "CXLCmd"]

__version__ = "1.0.0"
