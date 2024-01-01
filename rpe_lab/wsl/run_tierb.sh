#!/bin/bash
# Tier-B run: constructed delay between Query and Get (plan section 6).
# Starts a standard driver run (victim seeds + pressure churns) and attaches
# rpe_probe as delayed readers against the same hot set.
# Usage: run_tierb.sh <driver_config.yaml> <delay_ms> [probe_rate] [bypass]
set -u
RPE="${RPE_LAB:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CFG=$1
DELAY=$2
RATE=${3:-4}
BYPASS=${4:-0}
[ "$BYPASS" = "1" ] && export RPE_LAB_BYPASS_GUARD=1
RUN_ID=$(python3 -c "import json; print(json.load(open('$RPE/configs/$CFG'))['run_id'])")
DUR=$(python3 -c "import json; print(json.load(open('$RPE/configs/$CFG'))['duration_s'])")
LEDGER="$RPE/results/ledger_$RUN_ID.json"

cd "$RPE"
python3 driver.py run --config "configs/$CFG" > "results/tierb_driver_$RUN_ID.log" 2>&1 &
DRIVER=$!
echo "driver run pid=$DRIVER run_id=$RUN_ID delay=${DELAY}ms"

for i in $(seq 1 120); do
    [ -s "$LEDGER" ] && break
    sleep 2
done
[ -s "$LEDGER" ] || { echo "ledger never appeared"; kill $DRIVER; exit 1; }
sleep 5
rm -f "$RPE/results/probe_${RUN_ID}_p0.jsonl"  # avoid mixing runs (probe appends)

"$RPE/probe/rpe_probe" --master=127.0.0.1:50051 --ledger="$LEDGER" \
    --delay_ms="$DELAY" --duration_s="$DUR" --rate="$RATE" \
    --out="$RPE/results/probe_${RUN_ID}_p0.jsonl" --mount_mb=0 \
    > "results/probe_${RUN_ID}.log" 2>&1 &
PROBE=$!
echo "probe pid=$PROBE"

wait $DRIVER
kill $PROBE 2>/dev/null
if [ "$BYPASS" = "1" ]; then
    python3 analysis/tierb_aggregate.py "$RUN_ID" "" bypass
else
    python3 analysis/tierb_aggregate.py "$RUN_ID"
fi
echo "TIERB_DONE $RUN_ID"
