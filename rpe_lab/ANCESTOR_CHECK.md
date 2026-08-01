# Ancestor check record（批注 29 证据链）

日期：2026-07-29。执行环境：Windows 11 宿主机，curl + GitHub REST API（本机 git 协议被运营商干扰，见 ENV.md；compare API 的 merge_base 判定与 `git merge-base --is-ancestor` 等价）。

## 被审对象

- 上游仓库：https://github.com/kvcache-ai/Mooncake （main 分支）
- TESTED_COMMIT：`f20b7061097e4e2fda825f4106f215c71f13274a`（main @ 2026-07-17T10:48:09Z；全树 1371 blob 校验零差异，见 manifest.md）

## PR #2447 元数据

- API：`GET https://api.github.com/repos/kvcache-ai/Mooncake/pulls/2447`
- 标题：`[Store] Fix stale hot cache reuse after object removal`
- state: closed，merged: true，merged_at: `2026-06-16T03:56:04Z`
- merge_commit_sha：`75315dedaf7d7e04f18944193f6c58fb3a5b9f5e`

## Ancestor check（compare API 输出）

命令：

```
curl -s "https://api.github.com/repos/kvcache-ai/Mooncake/compare/75315dedaf7d7e04f18944193f6c58fb3a5b9f5e...f20b7061097e4e2fda825f4106f215c71f13274a"
```

关键字段（完整响应 2,621,556 字节，未随档保存）：

```
status:        ahead
ahead_by:      229
behind_by:     0
total_commits: 229
merge_base:    75315dedaf7d7e04f18944193f6c58fb3a5b9f5e
```

结论：`behind_by = 0` 且 merge_base 即 PR #2447 的 merge commit 本身，等价于
`git merge-base --is-ancestor 75315dedaf7d7e04f18944193f6c58fb3a5b9f5e f20b7061097e4e2fda825f4106f215c71f13274a`
返回真。**TESTED_COMMIT 包含 PR #2447 合入的 stale hot-cache 修复。**

## 插桩 patch 哈希（SHA-256）

`rpe_lab/patch/` 下四份文件：

```
89a1c459c01f95ce68b94e673d680f5c7c940d72b538884a911cebeff4cad769  client_service_rpe_lab.patch
cc42167c4ff905063c15a6c6745a023b98fd2cbd8d171c9c2d11c86e739318bb  client_service_rpe_lab_bypass.patch
c1df36052388aa4cf738613dd53740eb6ce729101faf3fdf1d8c3ac01fc361f1  master_service_rpe_lab_evictlog.patch
39961dfc6c4b0f32a858d68fea8a1a8f747277f335d80a8095e2c824fc1a63b8  rpe_lab_probe.h
```
