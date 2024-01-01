# RPE-lab 数据与来源清单（manifest）

## 代码

- **上游仓库**: https://github.com/kvcache-ai/Mooncake
- **TESTED_COMMIT**: `f20b7061097e4e2fda825f4106f215c71f13274a`（main @ 2026-07-17T10:48:09Z）
- **来源与校验**: 实验起始于一份无 `.git` 的上游源码快照。
  通过 GitHub git/trees API 递归树（1371 个 blob）逐文件 SHA-1 比对，
  快照与上述 commit **完全一致**（0 missing / 0 mismatch / 0 extra）。
  校验脚本：`rpe_lab/wsl/verify_tree.py`、`rpe_lab/wsl/refine_commit.py`。
- **子模块**（gitlink pin 来自 commit 树，codeload tarball 下载，gzip 校验通过）:
  - `extern/pybind11` @ `58c382a8e3d7081364d2f5c62e7f429f0412743b`
  - `extern/yalantinglibs` @ `6a0e067d9a43492cf8e4e280b531924fbd724dbd`
- **WSL 工作副本**: `~/mooncake`（git init 导入，`rpe-lab` 分支；import commit 信息记录上游 SHA）

## Trace

- **文件**: `rpe_lab/trace/BurstGPT_1.csv`
- **来源**: https://raw.githubusercontent.com/HPMLL/BurstGPT/main/data/BurstGPT_1.csv
  （即 BurstGPT Release v2.0 的 `BurstGPT_1.csv`，头两个月 trace，含失败请求）
- **大小**: 50,853,373 字节，1,429,738 行（1 行表头 + 1,429,737 条请求）
- **SHA256**: `46fc9480ef0b748ecb2b51d512ff08c196b031782cbe6f78e28044d768e86d5a`
- **使用方式（去标识）**: driver 仅取 `Timestamp`（计算 IAT 序列）与
  `Total tokens`（上下文长度，用于记账/分层统计）；不使用任何会话/用户标识。
  该文件为 v1 schema（无 Session ID 列），driver 以行号代替会话键。
- **引用**: Wang et al., BurstGPT, KDD'25, https://doi.org/10.1145/3711896.3737413

## 本体改动（唯一）

- `mooncake-store/src/client_service.cpp` 三处 lease-expiry 分支前插入被动日志调用，
  新增头文件 `mooncake-store/src/rpe_lab_probe.h`（只读观测、零行为改变）。
  patch 存根：`rpe_lab/patch/`（探针头文件、单元测试、git format-patch 导出）。
  单独 commit，见 WSL 工作副本 `git log`。
