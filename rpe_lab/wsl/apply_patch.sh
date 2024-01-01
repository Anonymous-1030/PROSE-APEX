#!/bin/bash
# Phase 2: apply the passive-instrumentation patch as its own commit,
# rebuild incrementally, and re-verify the Python import.
set -euo pipefail
RPE_LAB="${RPE_LAB:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MOONCAKE_REPO="${MOONCAKE_REPO:-$HOME/mooncake}"
RPE="$RPE_LAB"
cd "$MOONCAKE_REPO"

echo "== current branch (must be rpe-lab) =="
git branch --show-current

echo "== apply patch + probe header =="
git apply --check "$RPE/patch/client_service_rpe_lab.patch"
git apply "$RPE/patch/client_service_rpe_lab.patch"
cp "$RPE/patch/rpe_lab_probe.h" mooncake-store/src/rpe_lab_probe.h

echo "== git diff stat (should be client_service.cpp only, small) =="
git diff --stat
git add mooncake-store/src/client_service.cpp mooncake-store/src/rpe_lab_probe.h
git -c user.email=rpe@lab -c user.name=rpe-lab commit -m \
"rpe-lab: passive discard-point logging (no behavior change)

Inserts rpe_lab::LogGetDiscard before the three lease-expiry discard
branches in client_service.cpp (Client::Get x2, Client::BatchGet).
Active only when RPE_LAB_EVENTS env var is set; appends one JSONL
event per discard with the 64-byte identity header parsed from the
bytes about to be discarded. Probe is header-only
(mooncake-store/src/rpe_lab_probe.h), no master changes, no changes
to any control/data path logic."
git log --oneline | head -3

echo "== incremental rebuild =="
cd build
make -j"$(nproc)" mooncake_store mooncake_master store 2>&1 | tail -5
sudo -n make install 2>&1 | tail -3
python3 -c "from mooncake.store import MooncakeDistributedStore; print('PYBIND_IMPORT_OK_AFTER_PATCH')"
echo "PATCH_BUILD_DONE"
