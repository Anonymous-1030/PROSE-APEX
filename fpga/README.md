# APEX U280 FPGA Prototype

Alveo U280 prototyping wrapper for the APEX scoring pipeline and CEFE
front-end modules. Targets **250 MHz** (4.0 ns period) on part
`xcu280-fsvh2892-2L-e`.

## Build

```bash
vivado -mode batch -source synth_u280.tcl -tclargs -jobs 8
```

Output: `./u280_apex_build/u280_apex.bit`

## Architecture

```text
SLR0: Clock Wizard (300→250 MHz) + AXI-Lite CSR + PCIe/XDMA interface
SLR1: APEX Pipeline (scoring datapath — MAC, heap, weight update)
SLR2: CEFE front-end (VC-WRR, CFO CAM, BDB parser)
```

SLR boundary crossing uses Laguna pipeline registers on the WRR→Pipeline
path for timing closure.

## Modules Instantiated

| Module | File | Function |
| ------ | ---- | -------- |
| `u_bdb_parser` | `cefe_bdb_parser.sv` | Batch Descriptor Block DMA + parse |
| `u_cfo_cam` | `cefe_cfo_cam.sv` | Cross-tenant Fetch-Once coalescence |
| `u_vc_wrr` | `cefe_vc_wrr.sv` | 16-VC deficit weighted round-robin |
| `u_apex_pipeline` | `APEX_PIPELINE.sv` | 9-stage scoring pipeline |

## Prototyping Simplifications

The FPGA prototype makes the following simplifications relative to the
full ASIC RTL:

- **HMAC Verification Bypass:** The 64-bit HMAC-SHA256 tag verification is
  hardwired to always-pass (`hmac_rsp_pass = 1'b1`). The cross-tenant trust
  model is fully implemented and validated in the ASIC RTL (`cefe_cfo_cam.sv`
  HIT path: single-cycle 64-bit equality check; MISS path: external SHA-256
  accelerator round-trip). The FPGA bypasses this to eliminate the need for an
  external HMAC accelerator IP in the prototyping build.

- **DMA Loopback:** The CFO CAM DMA read interface is tied to always-ready
  (loopback mode). In production, this connects to the HBM AXI controller.

- **Single-VC Mode:** Only VC0 is connected to the PCIe descriptor stream
  for simplified bring-up. All 16 VCs are structurally present and can be
  driven via ILA/VIO for multi-tenant testing.

- **Feedback Tied Off:** The GPU feedback interface (`fb_*`) is tied to zero.
  Use the AXI-Lite scratch register or ILA injection for feedback testing.

## Constraints

- `u280_constraints.xdc`: Pin assignments, clock definitions, SLR pblocks,
  false paths for async feedback and quasi-static config registers.
- Target: 250 MHz with positive WNS (script aborts on timing violation).

## CSR Register Map

See `u280_top.sv` lines 138–155 for the full AXI-Lite register map
(16 × 32-bit registers at BAR0 offset 0x00–0x3C).
