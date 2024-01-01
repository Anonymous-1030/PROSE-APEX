# RPE-lab Runbook (RUNBOOK)

All commands run inside WSL Ubuntu (default distribution, enter with `wsl`).
Working directory: `RPE_LAB=<path to this rpe_lab/ directory>` (exported; the
helper scripts in `wsl/` and `probe/` self-locate it from their own path when
unset). `MOONCAKE_REPO` defaults to `$HOME/mooncake`, `MOONCAKE_BUILD` to
`$MOONCAKE_REPO/build`.

## 0. Prerequisites (one-time)

- Repo `$MOONCAKE_REPO` (`rpe-lab` branch; import commit = upstream `f20b706`,
  instrumentation commit on top — see `git log`)
- Build: `bash $RPE_LAB/wsl/build_wsl.sh`
  (incremental rebuild: `cd $MOONCAKE_BUILD && make -j12 store && sudo make install`)
- Trace: `$RPE_LAB/trace/BurstGPT_1.csv` (SHA256 recorded in manifest.md; already
  checked in)

## 1. Long runs (surviving session disconnects)

```bash
tmux new -s rpe
# run the matrix commands below inside tmux; Ctrl+b d to detach,
# tmux a -t rpe to reattach
```

Do not use `wsl.exe -e nohup`: WSL session reclamation kills the process tree.

## 2. Phase 4 Tier-A full matrix (54 cells x 2h each)

```bash
cd $RPE_LAB
ls configs/tierA_*.yaml | xargs -n1 basename | tr '\n' ' ' > /tmp/cells.txt
bash wsl/run_matrix.sh $(cat /tmp/cells.txt)
# single cell:  python3 driver.py run --config configs/tierA_evictaggr_ttl5000_c64_seed42.yaml
# aggregation:  python3 analysis/aggregate.py
```

Each finished cell writes `results/tierA_<run_id>.json` + `events_<run_id>.jsonl`
+ master log. If a cell reports `guard_fires=0`, adjust the tc rate for that TTL
(`TC_BY_TTL` in `configs/gen_configs.py`) per the window formula in
results/NOTES.md, then regenerate the configs with `python3 configs/gen_configs.py`.

## 3. 24h default-TTL cell (plan Phase 6 DoD target)

```bash
# create a 24h config (duration_s=86400):
python3 - << 'EOF'
import json
c = json.load(open('configs/tierA_evictaggr_ttl5000_c64_seed42.yaml'))
c['run_id'] = 'tierA_evictaggr_ttl5000_c64_seed42_24h'
c['duration_s'] = 86400
json.dump(c, open('configs/tierA_evictaggr_ttl5000_c64_seed42_24h.yaml', 'w'), indent=2)
EOF
bash wsl/run_matrix.sh tierA_evictaggr_ttl5000_c64_seed42_24h.yaml
```

## 4. Tier-B (constructed delay, probe binary)

```bash
# build rpe_probe (if not yet built):
bash probe/build_probe.sh
# start master + the two tenants (any tierA-style config; the probe attaches):
python3 driver.py run --config configs/tierB_d2000_ttl1000_c64_seed42.yaml &
# the probe reads the same ledger and injects the delay:
./probe/rpe_probe --master=127.0.0.1:50051 \
  --ledger=results/ledger_tierB_d2000_ttl1000_c64_seed42.json \
  --delay_ms=2000 --duration_s=7200 --rate=2 \
  --out=results/probe_tierB_d2000_ttl1000_c64_seed42_p0.jsonl
# aggregation: python3 analysis/tierb_aggregate.py tierB_d2000_ttl1000_c64_seed42
# delay sweep: X in {TTL/2, TTL, 2*TTL} (e.g. TTL=1000 -> 500/1000/2000ms)
```

(`wsl/run_tierb.sh <driver_config.yaml> <delay_ms> [probe_rate]` automates the
driver + probe pairing.)

## 5. Phase 5 pin cliff (re-run longer once a 30-min pair exists)

```bash
bash wsl/run_matrix.sh expB_unpinned_ttl5000_c64.yaml expB_pinned_ttl5000_c64.yaml
```

## 6. Red line

If any run reports `success_mismatch > 0`, stop the matrix immediately, preserve
all of `results/` plus the master binary and logs, and follow responsible
disclosure.
