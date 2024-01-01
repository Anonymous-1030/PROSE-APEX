#!/bin/bash
# Validate rpe_probe end-to-end. The putting client must STAY ALIVE
# (its segment hosts the object); run probes against the live holder.
set -u
RPE="${RPE_LAB:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MOONCAKE_BUILD="${MOONCAKE_BUILD:-$HOME/mooncake/build}"
export RPE_LAB="$RPE"
R="$RPE/results"
pkill -f mooncake_master 2>/dev/null; sleep 1

"$MOONCAKE_BUILD/mooncake-store/src/mooncake_master" \
  --rpc_port=50051 --eviction_high_watermark_ratio=0.5 \
  --eviction_ratio=0.1 --default_kv_lease_ttl=1000 \
  --allow_evict_soft_pinned_objects=1 > "$R/probe_smoke_master.log" 2>&1 &
MPID=$!
sleep 3

python3 - << 'EOF' &
import os, sys, time, json
sys.path.insert(0, os.environ["RPE_LAB"])
import rpe_header as rh
from mooncake.store import MooncakeDistributedStore
s = MooncakeDistributedStore()
assert s.setup("localhost", "P2PHANDSHAKE", 2*1024**3, 16*1024**2, "tcp", "", "127.0.0.1:50051") == 0
key = "hot/0000"
assert s.put(key, rh.make_payload(1, key, 1, time.time_ns(), 3670016)) == 0
json.dump({"keys": {"hot/0000": {"gen": 1}}}, open('/tmp/probe_ledger.json', 'w'))
print("HOLDER_PUT_OK", flush=True)
time.sleep(60)  # keep the segment (and the object) alive
EOF
HOLDER=$!
sleep 6

echo "== delay_ms=2000 (TTL=1000): expect rc=-707 =="
"$RPE/probe/rpe_probe" --master=127.0.0.1:50051 --ledger=/tmp/probe_ledger.json \
  --delay_ms=2000 --duration_s=8 --rate=2 --out=/tmp/probe_d2000.jsonl --mount_mb=0 2>/dev/null
tail -2 /tmp/probe_d2000.jsonl

echo "== delay_ms=0: expect rc=0 =="
"$RPE/probe/rpe_probe" --master=127.0.0.1:50051 --ledger=/tmp/probe_ledger.json \
  --delay_ms=0 --duration_s=5 --rate=5 --out=/tmp/probe_d0.jsonl --mount_mb=0 2>/dev/null
tail -2 /tmp/probe_d0.jsonl

kill $HOLDER $MPID 2>/dev/null; wait 2>/dev/null
echo PROBE_SMOKE_DONE
