# Gated-Pull End-to-End Throughput Runbook

**Goal:** Measure the end-to-end tok/s of the gated-pull (commodity Type-3 CXL + Alveo U280) deployment and compare it against a fetch-then-score baseline on the same silicon. The resulting ratio is the end-to-end tok/s figure the paper's deployment section reports once measured on physical silicon.

**Expected outcome:** A measured end-to-end throughput ratio (with 95% CI) and an estimate of how much of the gap from the projected 3.1× endpoint-DMA ratio is explained by the +2–5 µs/batch admission cost.

## Platform

- Host server with commodity Type-3 CXL memory expander(s) attached.
- Xilinx Alveo U280 FPGA in the same host, programmed with the gated-pull CEFE data path (see `fpga/u280_top.sv` and `host_sw/`).
- 8 logical hosts (tenants) multiplexed over the physical setup; 2 GB/s per tenant offered load.
- Workload: Qwen2.5-7B, 64K-token context, 64-token chunks, 256 decode steps.
- Oversubscription: 16× (1024 candidates / 32-chunk admit budget).

## Software setup

1. Build the host-side runtime:
   ```bash
   cd host_sw
   make bench_modeb_e2e
   ```
2. Program the FPGA with the gated-pull bitstream (consult `fpga/README.md` for the specific bitstream target).
3. Verify CXL Type-3 enumeration and BAR/IOMMU/PASID/ACS configuration as noted in `docs/SIMCXL_EXTENSION.md`.

## Baseline run (fetch-then-score)

1. Disable the endpoint admission gate in the host runtime (or use the `--no-gate` flag if available).
2. Run 3 seeds:
   ```bash
   ./bench_modeb_e2e \
     --hosts 8 --bw-gbps 2.0 --model qwen2.5-7b \
     --context 64k --chunks 64 --steps 256 --oversub 16 \
     --seed 1000 --output baseline_s0.json
   # repeat for seeds 1001, 1002
   ```
3. Record end-to-end tok/s and per-batch latency for each seed.

## Gated-pull run

1. Enable the endpoint admission gate in the host runtime (or use the `--gated-pull` flag).
2. Run 3 seeds with the same parameters:
   ```bash
   ./bench_modeb_e2e \
     --hosts 8 --bw-gbps 2.0 --model qwen2.5-7b \
     --context 64k --chunks 64 --steps 256 --oversub 16 \
     --seed 1000 --gated-pull --output gated_s0.json
   # repeat for seeds 1001, 1002
   ```
3. Record end-to-end tok/s and per-batch latency for each seed.

## Analysis

1. Compute the mean and 95% CI for baseline tok/s and gated-pull tok/s across the 3 seeds.
2. Compute the ratio: `mean(gated_tok_s) / mean(baseline_tok_s)`.
3. Compute the mean per-batch admission overhead from the gated-pull latency log (expect +2–5 µs/batch).
4. Estimate the throughput impact of that overhead at the measured batch rate and compare it to the gap between the measured ratio and the projected 3.1× endpoint-DMA ratio.
5. Save the summary as `results/gated_pull_e2e_summary.json` with the following schema:
   ```json
   {
     "config": { "hosts": 8, "bw_gbps_per_host": 2.0, "model": "qwen2.5-7b", "context": "64k", "oversub": 16 },
     "baseline_tok_s": [ ..., ..., ... ],
     "gated_tok_s": [ ..., ..., ... ],
     "ratio": 0.0,
     "ratio_ci_95": [0.0, 0.0],
     "admission_us_per_batch": 0.0,
     "notes": "..."
   }
   ```

## Success criteria

- The ratio and 95% CI are reported for all 3 seeds.
- The +2–5 µs/batch admission cost is quantified and its expected throughput impact is stated.
- Any missing FPGA/host-software flag that prevents a clean run is recorded in `REVISION_TODOS.md` with the exact command that failed.
