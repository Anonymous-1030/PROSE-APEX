#!/bin/bash
# Reconstruct the Mooncake repo inside WSL from a verified local snapshot
# (tree verified blob-for-blob against upstream commit by verify_tree.py)
# plus the two submodule codeload tarballs. Then git init + rpe-lab branch.
# Usage: MOONCAKE_SNAPSHOT=/path/to/Mooncake-main bash setup_repo2.sh
#   (expects /tmp/pybind11.tar.gz and /tmp/ylt.tar.gz)
set -euo pipefail

SHA=f20b7061097e4e2fda825f4106f215c71f13274a
SNAP="${MOONCAKE_SNAPSHOT:?set MOONCAKE_SNAPSHOT to the local Mooncake source snapshot}"
WORK="${MOONCAKE_REPO:-$HOME/mooncake}"

echo "== copy snapshot -> $WORK (excludes rpe_lab/)"
rm -rf "$WORK"
mkdir -p "$WORK"
tar -C "$SNAP" --exclude=./rpe_lab -cf - . | tar -C "$WORK" -xf -

echo "== extract submodule tarballs"
[ -s /tmp/pybind11.tar.gz ] || { echo "missing /tmp/pybind11.tar.gz"; exit 1; }
[ -s /tmp/ylt.tar.gz ] || { echo "missing /tmp/ylt.tar.gz"; exit 1; }
tar -xzf /tmp/pybind11.tar.gz -C "$WORK/extern/pybind11" --strip-components=1
tar -xzf /tmp/ylt.tar.gz -C "$WORK/extern/yalantinglibs" --strip-components=1
ls "$WORK/extern/pybind11/include/pybind11/pybind11.h" "$WORK/extern/yalantinglibs/CMakeLists.txt"

cd "$WORK"
git init -q
git add -A
git -c user.email=rpe@lab -c user.name=rpe-lab commit -qm \
  "import upstream kvcache-ai/Mooncake@$SHA (snapshot verified blob-for-blob via git trees API; submodule contents from codeload tarballs at pinned gitlinks)"
git checkout -qb rpe-lab
echo "== git state =="
git log --oneline
git status --short | head -5
du -sh "$WORK"
