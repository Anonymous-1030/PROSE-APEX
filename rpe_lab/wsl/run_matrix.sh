#!/bin/bash
# Serially run rpe_lab configs, one after another; safe under nohup for
# multi-day matrix execution. Usage: run_matrix.sh cfg1.yaml [cfg2.yaml ...]
set -u
RPE="${RPE_LAB:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$RPE"
mkdir -p results
for cfg in "$@"; do
    echo "=== $(date '+%F %T') START $cfg" | tee -a results/matrix.log
    python3 driver.py run --config "configs/$cfg" >> results/matrix.log 2>&1
    echo "=== $(date '+%F %T') DONE $cfg (rc=$?)" | tee -a results/matrix.log
done
echo "ALL_RUNS_DONE" | tee -a results/matrix.log
