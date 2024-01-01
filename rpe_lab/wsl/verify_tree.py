#!/usr/bin/env python3
"""Verify the D: snapshot tree == upstream commit tree, completely.

Fetches the full recursive git tree of the tested commit from the GitHub API
(one request, includes every blob SHA), then recomputes git-blob SHA-1 for
every file in the snapshot and compares. No git clone needed.

Usage: verify_tree.py <commit_sha> <snapshot_dir>
Exit 0 only if every tracked blob matches. Extras local to the snapshot
(e.g. rpe_lab/) are reported but non-fatal; empty dirs are ignored.
"""

import hashlib
import json
import os
import subprocess
import sys

SKIP_PREFIXES = ("rpe_lab/",)          # our own lab files
GITLINKS = {}                          # filled from tree (extern/*)
ALLOWED_EMPTY = ("extern/pybind11", "extern/yalantinglibs")  # empty in snapshot


def git_blob_sha(path):
    with open(path, "rb") as f:
        data = f.read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def main():
    sha, snap = sys.argv[1], sys.argv[2]
    out = subprocess.check_output([
        "curl", "-s", "-m", "120",
        f"https://api.github.com/repos/kvcache-ai/Mooncake/git/trees/{sha}?recursive=1"])
    tree = json.loads(out)
    if "tree" not in tree:
        sys.exit(f"API error: {tree}")
    if tree.get("truncated"):
        sys.exit("tree response truncated -- cannot verify fully")

    want = {}
    for e in tree["tree"]:
        if e["type"] == "blob":
            want[e["path"]] = e["sha"]
        elif e["type"] == "commit":
            GITLINKS[e["path"]] = e["sha"]

    mismatched, missing = [], []
    for path, sha in want.items():
        if path.startswith(SKIP_PREFIXES):
            continue
        local = os.path.join(snap, path)
        if not os.path.isfile(local):
            missing.append(path)
            continue
        if git_blob_sha(local) != sha:
            mismatched.append(path)

    extras = []
    for root, dirs, files in os.walk(snap):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), snap).replace(os.sep, "/")
            if rel.startswith(SKIP_PREFIXES):
                continue
            if rel not in want:
                extras.append(rel)

    print(f"tracked blobs: {len(want)}")
    print(f"missing in snapshot: {len(missing)}")
    for p in missing[:20]:
        print(f"  MISSING {p}")
    print(f"content mismatches: {len(mismatched)}")
    for p in mismatched[:20]:
        print(f"  MISMATCH {p}")
    print(f"extras (non-fatal): {len(extras)}")
    for p in extras[:20]:
        print(f"  EXTRA {p}")
    print("gitlinks (submodule pins):")
    for k, v in GITLINKS.items():
        print(f"  {k} {v}")

    if missing or mismatched:
        sys.exit("TREE VERIFICATION FAILED")
    print("TREE VERIFICATION OK")


if __name__ == "__main__":
    main()
