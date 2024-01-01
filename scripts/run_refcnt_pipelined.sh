#!/usr/bin/env bash
# REFCNT pipelined-atomic sensitivity sweep for P1-3.
# Outputs go to experiments/out/refcnt_pipelined/.
set -euo pipefail
cd "$(dirname "$0")/.."
python experiments/run_refcnt_pipelined.py
