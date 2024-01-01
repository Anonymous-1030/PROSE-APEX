#!/bin/bash
# Build the rpe_probe Tier-B binary (standalone, links libmooncake_store).
set -euo pipefail
MC="${MOONCAKE_REPO:-$HOME/mooncake}"
RPE="${RPE_LAB:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
OUT="$(dirname "$0")/rpe_probe"

g++ -std=c++20 -O2 -Wall \
  -I"$MC/mooncake-store/include" \
  -I"$MC/mooncake-store/include/cachelib_memory_allocator" \
  -I"$MC/mooncake-store/include/cachelib_memory_allocator/include" \
  -I"$MC/mooncake-store/include/cachelib_memory_allocator/fake_include" \
  -I"$MC/mooncake-transfer-engine/include" \
  -I"$MC/mooncake-common" \
  -I"$RPE/patch" \
  "$RPE/probe/rpe_probe.cpp" \
  -Wl,--start-group \
  "$MC/build/mooncake-store/src/libmooncake_store.a" \
  "$MC/build/mooncake-store/src/cachelib_memory_allocator/libcachelib_memory_allocator.a" \
  "$MC/build/mooncake-transfer-engine/src/libtransfer_engine.a" \
  "$MC/build/mooncake-transfer-engine/src/common/base/libbase.a" \
  "$MC/build/mooncake-common/src/libmooncake_common.a" \
  -Wl,--end-group \
  -L/usr/local/lib -lasio \
  -lgrpc++ -lgrpc -lprotobuf -lgpr -lcares -labsl_strings -labsl_status \
  -lglog -lgflags -lyaml-cpp -ljsoncpp -lhiredis -lcurl -lssl -lcrypto \
  -lnuma -libverbs -luring -lz -lzstd -lmsgpackc -lxxhash -lpthread -ldl \
  -o "$OUT"
echo "built: $OUT"
"$OUT" 2>&1 | head -2 || true
