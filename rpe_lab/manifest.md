# RPE-lab Data and Provenance Manifest (manifest)

## Code

- **Upstream repository**: https://github.com/kvcache-ai/Mooncake
- **TESTED_COMMIT**: `f20b7061097e4e2fda825f4106f215c71f13274a`
  (main @ 2026-07-17T10:48:09Z), also recorded in `TESTED_COMMIT.txt`
- **Source and verification**: the experiments started from a source snapshot
  without `.git`. The snapshot was compared file-by-file (git-blob SHA-1)
  against the recursive git tree of the above commit fetched through the
  GitHub git/trees API (1371 blobs): **identical** (0 missing / 0 mismatch /
  0 extra). Verification scripts: `rpe_lab/wsl/verify_tree.py`,
  `rpe_lab/wsl/refine_commit.py`.
- **Submodules** (gitlink pins taken from the commit tree, downloaded as
  codeload tarballs, gzip integrity checked):
  - `extern/pybind11` @ `58c382a8e3d7081364d2f5c62e7f429f0412743b`
  - `extern/yalantinglibs` @ `6a0e067d9a43492cf8e4e280b531924fbd724dbd`
- **WSL working copy**: `$HOME/mooncake` (`git init` import, `rpe-lab` branch;
  the import commit message records the upstream SHA)

## Trace

- **File**: `rpe_lab/trace/BurstGPT_1.csv`
- **Source**: https://raw.githubusercontent.com/HPMLL/BurstGPT/main/data/BurstGPT_1.csv
  (the `BurstGPT_1.csv` of BurstGPT Release v2.0, first-two-months trace,
  includes failed requests)
- **Size**: 50,853,373 bytes, 1,429,738 lines (1 header + 1,429,737 requests)
- **SHA256**: `46fc9480ef0b748ecb2b51d512ff08c196b031782cbe6f78e28044d768e86d5a`
- **Usage (de-identified)**: the driver reads only `Timestamp` (to build the
  IAT sequence) and `Total tokens` (context length, for accounting/tiered
  statistics); no session/user identifier is used. The file is v1 schema (no
  Session ID column); the driver uses the row number as the session key.
- **Citation**: Wang et al., BurstGPT, KDD'25, https://doi.org/10.1145/3711896.3737413

## Only source-tree modification

- `mooncake-store/src/client_service.cpp`: a passive logging call inserted
  before each of the three lease-expiry branches, plus the new header
  `mooncake-store/src/rpe_lab_probe.h` (read-only observation, zero behavior
  change). Patch artifacts: `rpe_lab/patch/` (probe header, unit test, git
  format-patch export). Committed separately; see `git log` in the WSL working
  copy.
