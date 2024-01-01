#!/usr/bin/env bash
# =============================================================================
# run_modeb_hw.sh — drive the single-host Mode B end-to-end benchmark on a
#                   commercial CXL Type-3 device and capture hardware provenance.
#
# This is the "real hardware" rebuttal artifact: it runs the Mode B
# (endpoint-gated pull) protocol against an actual commodity Type-3 CXL memory
# device and records RPE=0 + promotion latency on real CXL.mem traffic, next to
# a fetch-then-score control that DOES leak — so the RPE=0 is falsifiable.
#
# It also snapshots the machine's CXL topology (devdax devices, CXL NUMA nodes,
# dmesg CXL lines) into the output directory so a reviewer can confirm the
# numbers came from silicon, not a projection. If no CXL device is present the
# benchmark self-labels EMULATED and the provenance file records that plainly.
#
# Usage:
#   ./run_modeb_hw.sh [--devdax /dev/dax0.0] [--steps N] [-- <extra bench args>]
#
# Env:
#   PROSE_CXL_DEVDAX   devdax device path (overridden by --devdax)
#   PROSE_CXL_EMU_DIR  directory for emulated file backing (no real device)
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${HERE}/../experiments/out/modeb_hw"
mkdir -p "${OUT_DIR}"

DEVDAX=""
STEPS="500"
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --devdax) DEVDAX="$2"; shift 2 ;;
    --steps)  STEPS="$2"; shift 2 ;;
    --)       shift; EXTRA=("$@"); break ;;
    *)        EXTRA+=("$1"); shift ;;
  esac
done

BIN="${HERE}/bench_modeb_e2e"
if [[ ! -x "${BIN}" ]]; then
  echo "[run] building benchmark..."
  g++ -std=c++17 -O2 -Wall -Wextra -o "${BIN}" "${HERE}/bench_modeb_e2e.cpp"
fi

# --- Capture hardware provenance -------------------------------------------
PROV="${OUT_DIR}/hw_provenance.txt"
{
  echo "# PROSE-APEX Mode B — hardware provenance"
  echo "# captured: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host: $(uname -a)"
  echo
  echo "## devdax devices (/dev/dax*)"
  ls -l /dev/dax* 2>/dev/null || echo "  (none)"
  echo
  echo "## cxl devices (cxl list)"
  if command -v cxl >/dev/null 2>&1; then
    cxl list 2>/dev/null || echo "  (cxl present but list failed)"
  else
    echo "  (cxl CLI not installed)"
  fi
  echo
  echo "## daxctl regions (daxctl list)"
  if command -v daxctl >/dev/null 2>&1; then
    daxctl list 2>/dev/null || echo "  (daxctl present but list failed)"
  else
    echo "  (daxctl not installed)"
  fi
  echo
  echo "## NUMA topology (numactl -H)"
  if command -v numactl >/dev/null 2>&1; then
    numactl -H 2>/dev/null || echo "  (numactl failed)"
  else
    echo "  (numactl not installed)"
  fi
  echo
  echo "## kernel CXL messages (dmesg | grep -i cxl)"
  (dmesg 2>/dev/null | grep -i cxl | tail -40) || echo "  (no dmesg access or no CXL lines)"
} > "${PROV}" 2>&1 || true
echo "[run] hardware provenance -> ${PROV}"

# --- Run the benchmark ------------------------------------------------------
ARGS=(--steps "${STEPS}" --json)
if [[ -n "${DEVDAX}" ]]; then ARGS+=(--devdax "${DEVDAX}"); fi
if [[ ${#EXTRA[@]} -gt 0 ]]; then ARGS+=("${EXTRA[@]}"); fi

JSON_OUT="${OUT_DIR}/modeb_result.json"
LOG_OUT="${OUT_DIR}/modeb_run.log"

echo "[run] ${BIN} ${ARGS[*]}"
set +e
"${BIN}" "${ARGS[@]}" > "${JSON_OUT}" 2> "${LOG_OUT}"
RC=$?
set -e

cat "${LOG_OUT}"
echo
echo "[run] result JSON -> ${JSON_OUT}"
echo "[run] full log    -> ${LOG_OUT}"

REAL=$(grep -o '"real_cxl": *[a-z]*' "${JSON_OUT}" | awk '{print $2}')
if [[ "${REAL}" == "true" ]]; then
  echo "[run] SUBSTRATE: REAL CXL Type-3 device — these numbers are hardware-measured."
else
  echo "[run] SUBSTRATE: EMULATED — set PROSE_CXL_DEVDAX / --devdax on a CXL host for real numbers."
fi

exit ${RC}
