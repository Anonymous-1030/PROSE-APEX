# rpe_lab — Reclaimed-Payload Exposure harness for Mooncake Store

`rpe_lab` measures **Reclaimed-Payload Exposure (RPE)** in
[Mooncake Store](https://github.com/kvcache-ai/Mooncake): a client learns
replica locations via `GetReplicaList` and then reads asynchronously; if the
master evicts the object and reallocates its slot to a new object **before the
transfer completes**, the in-flight read can pull back the *new* object's
bytes. Mooncake's protection is the TTL lease guard: after lease expiry the
client discards the bytes it already read (`LEASE_EXPIRED`, -707). The harness
quantifies how often the guard trips (`guard_fires`), how many bytes are
fetched and discarded (`misbw_bytes`), and whether the discarded buffers
actually contained foreign object bytes (`rpe_events` — detected via a passive
probe inserted at the three discard branches, with zero behavior change).

Everything is pinned to upstream commit
`f20b7061097e4e2fda825f4106f215c71f13274a` (see `TESTED_COMMIT.txt`;
provenance and hash verification in `manifest.md`).

## Layout

- `driver.py` — workload driver (`smoke` / `seed` / `victim` / `pressure` /
  `run` / `reaggregate`). Two tenants: victim replays BurstGPT arrivals as
  Gets against a seeded hot set; pressure replays Puts for pool pressure.
  Configs are JSON-syntax `.yaml`; path fields accept `$MOONCAKE_BUILD` and
  `$RPE_LAB` env-var placeholders (expanded in `load_config`).
- `rpe_header.py`, `test_rpe_header.py` — payload identity-header format
  (magic / tenant / key_hash / generation) and its unit tests.
- `chimney.py` — Phase 2 end-to-end chimney (forced lease expiry -> discard
  event).
- `configs/` — experiment configs + `gen_configs.py` (regenerates the 54-cell
  Tier-A matrix: 2 variants x TTL {1000,5000,11000} x concurrency {32,64,128}
  x seed {42,43,44}).
- `patch/` — `rpe_lab_probe.h` (header-only passive probe),
  `client_service_rpe_lab.patch` (adds one logging call before each of the
  three lease-expiry discard branches in `client_service.cpp`), `test_probe.cpp`.
- `probe/` — `rpe_probe.cpp` Tier-B standalone probe (injects a constructed
  delay between Query and Get) + build/smoke scripts. The compiled `rpe_probe`
  binary is **not** checked in; build it with `probe/build_probe.sh`.
- `analysis/` — `aggregate.py` (Tier-A macros over `results/tierA_*.json`),
  `tierb_aggregate.py` (Tier-B classification over probe output).
- `results/` — checked-in headline outputs (see below), per-run configs,
  ledgers, event logs (`events_*.jsonl`), probe records
  (`probe_tierB_*.jsonl`), metrics time series, and `NOTES.md` (decisions,
  deviations, operational definitions).
- `trace/` — `BurstGPT_1.csv` (input workload; SHA256 in `manifest.md`).
- `wsl/` — WSL helper scripts: repo reconstruction (`setup_repo*.sh`,
  `verify_tree.py`, `refine_commit.py`, `match_commit.sh`), build
  (`build_wsl.sh`), patch application (`apply_patch.sh`), smoke/chimney runs,
  matrix and Tier-B runners. Scripts self-locate `$RPE_LAB` from their own
  path; override via `RPE_LAB`, `MOONCAKE_REPO`, `MOONCAKE_BUILD`,
  `MOONCAKE_SNAPSHOT` env vars.

Docs: `RUNBOOK.md` (how to run), `ENV.md` (environment record), `manifest.md`
(provenance), `audit_notes.md` (source audit with code anchors),
`smoke_log.md` (Phase 1/2 logs), `results/NOTES.md` (experiment decisions and
DoD ladder).

## Prerequisites

- A Mooncake Store build at commit `f20b706` producing
  `$MOONCAKE_BUILD/mooncake-store/src/mooncake_master`, `libmooncake_store.a`,
  and the Python bindings (`from mooncake.store import
  MooncakeDistributedStore`). Configure line and toolchain: `ENV.md`.
- Python 3.12+ (stdlib only for the driver and analysis scripts).
- The BurstGPT CSV is already included at `trace/BurstGPT_1.csv`.

## Quick start

```bash
export RPE_LAB="$(pwd)"                       # this rpe_lab/ directory
export MOONCAKE_BUILD="$HOME/mooncake/build"  # contains mooncake-store/src/mooncake_master

# 1. Build the master + client libs (inside WSL; see ENV.md)
bash wsl/build_wsl.sh

# 2. Apply the passive-instrumentation patch (own commit) and rebuild
bash wsl/apply_patch.sh

# 3. Smoke test, then a single experiment cell
bash wsl/smoke_run.sh
python3 driver.py run --config configs/tierA_evictaggr_ttl5000_c64_seed42.yaml

# 4. Aggregate
python3 analysis/aggregate.py                                   # Tier-A macros
python3 analysis/tierb_aggregate.py tierB_d2000_ttl1000_c64_seed42  # Tier-B
```

Full matrix, 24h cells, Tier-B delay sweeps, and the pin-cliff pair:
`RUNBOOK.md`.

## Checked-in headline outputs

All numbers below are computed from the checked-in `results/` files
(`analysis/aggregate.py` reproduces the Tier-A rows).

| Metric | Value | Source |
|---|---|---|
| Tier-A runs | 9 configs (9,005 s total) | `results/tierA_*.json` |
| Guard fires (lease-guard discards, rc=-707) | **32,908** | sum over 9 Tier-A runs |
| Successful reads | 81,649 | sum over 9 Tier-A runs |
| Total discards (Tier-A + Tier-B) | 32,983 | 32,908 + 75 |
| Peak guard fires in one 10-min config | 7,754 | `tierA_dod2_tc800m_...json` |
| Discarded bytes (misbw) | **120.8 GB** (peak 28.5 GB in one config = 49% of that config's read bandwidth) | sum over 9 Tier-A runs |
| Exposure events | **6 events, 22.0 MB = 0.0073%** of successful-read payload bytes | 1 natural (Tier-A run 6, `events_dod6_*.jsonl`) + 5 constructed (Tier-B) |
| Tier-B constructed window | 5 exposure events in a 2 s delay window over 7,200 iterations = **6.7% of the 75 binding-broken transfers** | `tierB_tierB_d2000_*.json`, `probe_tierB_d2000_..._p0.jsonl` |
| Overstay (binding broken mid-transfer) | per-configuration median up to **3.2 s at 1 s TTL** | run 5 (expired_by p50 = 3242 ms), `results/NOTES.md` |
| Hard-pin experiment (Phase 5) | **39.3%** pool non-reclaimable, eviction success **1.2%**, throughput **1.0683x** pinned/unpinned | `results/expB_pin_cliff.json` |

All runs report `success_mismatch = 0` (the red-line condition never tripped).

**Excluded from this release for size** (regenerable via `RUNBOOK.md`): raw
request-level logs (`results/req_pressure_*.jsonl`, `results/req_victim_*.jsonl`),
the master stdout logs (`results/mooncake_master.*`), the Tier-B driver stdout
log (`results/tierb_driver_*.log`), and the compiled `probe/rpe_probe` binary.
All aggregate `tier*.json` summaries, `events_*.jsonl` event logs,
`probe_tierB_*.jsonl` probe records, and `expB_pin_cliff.json` are included.
