#!/bin/bash
# One-off: identify which upstream commit a local source snapshot corresponds
# to, using only curl + GitHub API (no git clone needed).
# Usage: MOONCAKE_SNAPSHOT=/path/to/Mooncake-main bash match_commit.sh
set -u
SNAP="${MOONCAKE_SNAPSHOT:?set MOONCAKE_SNAPSHOT to the local Mooncake source snapshot}"
PROBE="mooncake-store/src/master_service.cpp"
VERIFY_FILES="CMakeLists.txt mooncake-store/src/client_service.cpp mooncake-store/src/offset_allocator.cpp mooncake-store/include/types.h mooncake-store/src/real_client.cpp"

H1=$(sha256sum "$SNAP/$PROBE" | cut -d' ' -f1)
echo "snapshot $PROBE sha256: $H1"

curl -s -m 30 "https://api.github.com/repos/kvcache-ai/Mooncake/commits?path=$PROBE&per_page=100" -o /tmp/rpe_commits.json
python3 -c "
import json
d = json.load(open('/tmp/rpe_commits.json'))
for c in d:
    print(c['sha'], c['commit']['committer']['date'])
" > /tmp/rpe_sha_list.txt
wc -l < /tmp/rpe_sha_list.txt

FOUND=""
while read -r sha date; do
    curl -s -m 30 "https://raw.githubusercontent.com/kvcache-ai/Mooncake/$sha/$PROBE" -o /tmp/rpe_probe.bin
    h=$(sha256sum /tmp/rpe_probe.bin | cut -d' ' -f1)
    if [ "$h" = "$H1" ]; then
        echo "CANDIDATE $sha $date (probe file matches)"
        # verify the other files at this commit
        ok=1
        for vf in $VERIFY_FILES; do
            curl -s -m 30 "https://raw.githubusercontent.com/kvcache-ai/Mooncake/$sha/$vf" -o /tmp/rpe_v.bin
            hv=$(sha256sum /tmp/rpe_v.bin | cut -d' ' -f1)
            hs=$(sha256sum "$SNAP/$vf" | cut -d' ' -f1)
            if [ "$hv" != "$hs" ]; then echo "  MISMATCH $vf"; ok=0; fi
        done
        if [ "$ok" = "1" ]; then FOUND=$sha; echo "FULL_MATCH $sha $date"; break; fi
    fi
done < /tmp/rpe_sha_list.txt

if [ -n "$FOUND" ]; then
    echo "$FOUND" > /tmp/rpe_tested_commit.txt
    curl -s -m 30 "https://api.github.com/repos/kvcache-ai/Mooncake/contents/extern?ref=$FOUND" -o /tmp/rpe_extern.json
    python3 -c "
import json
for e in json.load(open('/tmp/rpe_extern.json')):
    print(e['name'], e['type'], e.get('sha'))
"
else
    echo "NO_MATCH_FOUND"
fi
