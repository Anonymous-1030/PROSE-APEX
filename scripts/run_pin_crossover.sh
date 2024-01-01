#!/usr/bin/env bash
# Pin-table crossover sweep for P1-2.
# Outputs go to experiments/out/pin_crossover/.
set -euo pipefail
cd "$(dirname "$0")/.."
python experiments/run_pin_crossover.py
