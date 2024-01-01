#!/bin/bash
# One-off: reconstruct the Mooncake repo at the tested commit inside WSL
# without git-protocol access to GitHub (GnuTLS/Schannel both get RST here,
# curl/OpenSSL works). Uses codeload tarballs + local git init.
#
# Usage: setup_repo.sh <mooncake_sha> <pybind11_gitlink_sha> <yalantinglibs_gitlink_sha>
set -euo pipefail

SHA=${1:?mooncake commit sha}
PYBIND_SHA=${2:?pybind11 submodule sha}
YLT_SHA=${3:?yalantinglibs submodule sha}
WORK=$HOME/mooncake

echo "== fetching Mooncake@$SHA"
rm -rf "$WORK"; mkdir -p "$WORK"
curl -sL -m 1800 "https://codeload.github.com/kvcache-ai/Mooncake/tar.gz/$SHA" -o /tmp/mooncake.tar.gz
tar -xzf /tmp/mooncake.tar.gz -C "$WORK" --strip-components=1

echo "== fetching extern/pybind11@$PYBIND_SHA"
mkdir -p "$WORK/extern/pybind11"
curl -sL -m 900 "https://codeload.github.com/pybind/pybind11/tar.gz/$PYBIND_SHA" -o /tmp/pybind11.tar.gz
tar -xzf /tmp/pybind11.tar.gz -C "$WORK/extern/pybind11" --strip-components=1

echo "== fetching extern/yalantinglibs@$YLT_SHA"
mkdir -p "$WORK/extern/yalantinglibs"
curl -sL -m 900 "https://codeload.github.com/alibaba/yalantinglibs/tar.gz/$YLT_SHA" -o /tmp/ylt.tar.gz
tar -xzf /tmp/ylt.tar.gz -C "$WORK/extern/yalantinglibs" --strip-components=1

if [ -n "${MOONCAKE_SNAPSHOT:-}" ]; then
    echo "== verifying tree against local snapshot $MOONCAKE_SNAPSHOT"
    diff -r --brief "$WORK" "$MOONCAKE_SNAPSHOT" \
        -x rpe_lab -x .git -x .gitmodules -x '*.pyc' || true
fi

cd "$WORK"
git init -q
git add -A
git -c user.email=rpe@lab -c user.name=rpe-lab commit -qm \
    "import upstream kvcache-ai/Mooncake@$SHA (codeload tarball; git protocol unreachable from this host)"
git checkout -qb rpe-lab
echo "== done: $(git log --oneline | head -2)"
