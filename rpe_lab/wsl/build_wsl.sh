#!/bin/bash
# Build Mooncake (TCP-only usage; RDMA code may compile but is unused at runtime)
# inside WSL. Run AFTER setup_repo.sh. Produces:
#   - build/mooncake-store/src/mooncake_master
#   - C++ client library (libmooncake_store)
#   - Python bindings (from mooncake.store import MooncakeDistributedStore)
set -euo pipefail
cd "$HOME/mooncake"

echo "== [1/4] dependencies.sh (apt + yalantinglibs + go) =="
sudo -n bash dependencies.sh -y

echo "== [2/4] cmake configure =="
mkdir -p build && cd build
cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DWITH_STORE_RUST=OFF \
  -DBUILD_UNIT_TESTS=OFF \
  -DBUILD_EXAMPLES=OFF

echo "== [3/4] make =="
make -j"$(nproc)"

echo "== [4/4] install (python module -> site-packages, libs) =="
sudo -n make install

echo "== artifacts =="
ls -la mooncake-store/src/mooncake_master
find . -name 'libmooncake_store*' | head -5
python3 -c "from mooncake.store import MooncakeDistributedStore; print('PYBIND_IMPORT_OK')"
./mooncake-store/src/mooncake_master --help 2>&1 | head -5 || true
