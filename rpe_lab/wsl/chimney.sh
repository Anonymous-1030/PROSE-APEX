#!/bin/bash
# Phase 2 chimney: patched client + forced lease expiry -> discard event.
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
  --default_kv_lease_ttl=500 \
  --allow_evict_soft_pinned_objects=1 \
  > "$R/chimney_master.log" 2>&1 &
MPID=$!
sleep 3

export RPE_LAB_EVENTS="$R/events_chimney.jsonl"
export RPE_LAB_RUN_ID="chimney"
rm -f "$RPE_LAB_EVENTS"
python3 "$RPE_LAB/chimney.py"
RC=$?

kill $MPID 2>/dev/null
wait $MPID 2>/dev/null
echo "CHIMNEY_EXIT=$RC"
exit $RC
