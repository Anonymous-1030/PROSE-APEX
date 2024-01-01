#!/usr/bin/env bash
# Run the low-oversubscription optimistic-reclaim sweep needed for P1-2.
# Outputs go to experiments/out/optimistic_reclaim_low/.
set -euo pipefail
cd "$(dirname "$0")/.."
python experiments/run_oversub_low.py
