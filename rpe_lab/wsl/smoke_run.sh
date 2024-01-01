#!/bin/bash
# Phase 1 smoke: start master, run driver smoke, stop master.
set -u
RPE_LAB="${RPE_LAB:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MOONCAKE_BUILD="${MOONCAKE_BUILD:-$HOME/mooncake/build}"
R="$RPE_LAB/results"
mkdir -p "$R"
pkill -f mooncake_master 2>/dev/null; sleep 1

"$MOONCAKE_BUILD/mooncake-store/src/mooncake_master" \
  --rpc_port=50051 \
  --eviction_high_watermark_ratio=0.5 \
  --eviction_ratio=0.1 \
  --default_kv_lease_ttl=5000 \
  --allow_evict_soft_pinned_objects=1 \
  > "$R/smoke_master.log" 2>&1 &
MPID=$!
sleep 3
echo "== master startup (first 8 lines) =="
head -8 "$R/smoke_master.log"

python3 "$RPE_LAB/driver.py" smoke --config "$RPE_LAB/configs/smoke.yaml"
RC=$?

kill $MPID 2>/dev/null
wait $MPID 2>/dev/null
echo "== master log tail =="
tail -15 "$R/smoke_master.log"
echo "SMOKE_EXIT=$RC"
exit $RC
