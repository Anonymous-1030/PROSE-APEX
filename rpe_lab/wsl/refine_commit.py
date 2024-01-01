#!/usr/bin/env python3
"""Refine the tested-commit guess: the snapshot mostly matches d84d30a but
differs in 23 files, so it is probably a slightly newer commit on main.
Walk recent main history (newest first), fetch each commit's tree (1 API
call each), and compare blob SHAs for the differing paths only.

Usage: refine_commit.py <snapshot_dir> <known_good_sha> [paths...]
"""

import hashlib
import json
import os
import subprocess
import sys

DIFFERING = [
    ".github/workflows/pre-release.yaml",
    ".github/workflows/release-cuda13.yaml",
    ".github/workflows/release.yaml",
    "dependencies.sh",
    "docs/source/performance/sglang/sglang-benchmark-results-v1.md",
    "mooncake-store/include/storage_backend.h",
    "mooncake-store/src/http_metadata_server.cpp",
    "mooncake-store/src/storage_backend.cpp",
    "mooncake-store/tests/http_metadata_server_test.cpp",
    "mooncake-store/tests/storage_backend_test.cpp",
    "mooncake-transfer-engine/include/config.h",
    "mooncake-transfer-engine/include/transport/rdma_transport/rdma_context.h",
    "mooncake-transfer-engine/include/transport/rdma_transport/worker_pool.h",
    "mooncake-transfer-engine/src/config.cpp",
    "mooncake-transfer-engine/src/topology.cpp",
    "mooncake-transfer-engine/src/transfer_metadata.cpp",
    "mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp",
    "mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp",
    "mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp",
    "mooncake-transfer-engine/tests/rdma_endpoint_reestablish_test.cpp",
]
EXTRA = [
    "docs/source/image/sglang_pd_qwen3_235b_bandwidth.png",
    "docs/source/image/sglang_pd_qwen3_235b_transfer_time.png",
    "docs/source/image/sglang_pd_qwen3_235b_ttft_breakdown.png",
    "mooncake-transfer-engine/scripts/real_rdma_link_failover.sh",
    "mooncake-wheel/tests/test_release_wheel_tags.py",
]


def api(url):
    return json.loads(subprocess.check_output(["curl", "-s", "-m", "60", url]))


def git_blob_sha(path):
    with open(path, "rb") as f:
        data = f.read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def main():
    snap = sys.argv[1]
    local = {p: git_blob_sha(os.path.join(snap, p)) for p in DIFFERING + EXTRA}

    commits = api("https://api.github.com/repos/kvcache-ai/Mooncake/commits?per_page=100")
    print(f"scanning {len(commits)} recent commits on main...")
    for c in commits:
        sha = c["sha"]
        date = c["commit"]["committer"]["date"]
        tree = api(f"https://api.github.com/repos/kvcache-ai/Mooncake/git/trees/{sha}?recursive=1")
        if "tree" not in tree:
            print(f"  {sha[:8]} {date} tree fetch failed, skip")
            continue
        blobs = {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}
        bad = [p for p in DIFFERING if blobs.get(p) != local[p]]
        extra_missing = [p for p in EXTRA if p not in blobs]
        # EXTRA files exist in snapshot; at the right commit they must be tracked
        if not bad and not extra_missing:
            print(f"FULL_MATCH {sha} {date}")
            print(sha)
            return
        if len(bad) + len(extra_missing) <= 6:
            print(f"  near-miss {sha[:8]} {date}: {len(bad)} diff, {len(extra_missing)} missing-extra")
    print("NO_MATCH_IN_LAST_100")


if __name__ == "__main__":
    main()
