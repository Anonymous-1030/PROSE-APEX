# RPE-lab 运行手册（RUNBOOK）

所有命令在 WSL Ubuntu（默认发行版，`wsl` 进入）中执行。
工作目录：导出 `RPE_LAB` 指向本 `rpe_lab/` 目录（`wsl/` 与 `probe/` 下的辅助脚本在未设置时会按自身路径自动定位）。下文示例简写 `RPE=$RPE_LAB`。

## 0. 前置（已完成，勿重复）

- 仓库 `~/mooncake`（rpe-lab 分支；import commit = 上游 f20b706，插桩 commit 见 `git log`）
- 构建：`bash $RPE/wsl/build_wsl.sh`（已完成；增量重建 `cd ~/mooncake/build && make -j12 store && sudo make install`）
- trace：`$RPE/trace/BurstGPT_1.csv`（SHA256 见 manifest.md）

## 1. 长跑的正确姿势（防会话断开）

```bash
tmux new -s rpe
# 在 tmux 里执行下面的矩阵命令；Ctrl+b d 脱离，tmux a -t rpe 重连
```

不要用 `wsl.exe -e nohup`——WSL 会话回收会杀掉进程树（实测教训）。

## 2. Phase 4 Tier-A 全矩阵（54 格 × 2h ≈ 4.5 天）

```bash
cd $RPE
ls configs/tierA_*.yaml | xargs -n1 basename | tr '\n' ' ' > /tmp/cells.txt
bash wsl/run_matrix.sh $(cat /tmp/cells.txt)
# 单格：python3 driver.py run --config configs/tierA_evictaggr_ttl5000_c64_seed42.yaml
# 聚合：python3 analysis/aggregate.py
```

每格跑完自动写 `results/tierA_<run_id>.json` + `events_<run_id>.jsonl` + master 日志。
若某格 guard_fires=0：按 NOTES.md 的窗口公式调整该 TTL 的 tc 档（configs/gen_configs.py 里 TC_BY_TTL）。

## 3. 24h 默认档（计划 Phase 6 DoD 主目标）

```bash
# 先造一个 24h 配置（duration_s=86400），可用：
python3 - << 'EOF'
import json
c = json.load(open('configs/tierA_evictaggr_ttl5000_c64_seed42.yaml'))
c['run_id'] = 'tierA_evictaggr_ttl5000_c64_seed42_24h'
c['duration_s'] = 86400
json.dump(c, open('configs/tierA_evictaggr_ttl5000_c64_seed42_24h.yaml', 'w'), indent=2)
EOF
bash wsl/run_matrix.sh tierA_evictaggr_ttl5000_c64_seed42_24h.yaml
```

## 4. Tier-B（构造延迟，probe 二进制）

```bash
# rpe_probe 构建（若尚未构建）：
bash probe/build_probe.sh
# 启动 master + 两个 tenant（用任意 tierA 配置的 run，只是附加 probe）：
python3 driver.py run --config configs/tierB_d2000_ttl1000_c64.yaml &
# probe 读取同一 ledger，注入 delay：
./probe/rpe_probe --master=127.0.0.1:50051 \
  --ledger=results/ledger_tierB_d2000_ttl1000_c64.json \
  --delay_ms=2000 --duration_s=7200 --rate=2 \
  --out=results/probe_tierB_d2000_ttl1000_c64_p0.jsonl
# 聚合：python3 analysis/tierb_aggregate.py tierB_d2000_ttl1000_c64
# delay 扫描：X ∈ {TTL/2, TTL, 2×TTL}（例：TTL=1000 → 500/1000/2000ms）
```

## 5. Phase 5 pin 悬崖（已有一组 30min 结果时可复跑加长）

```bash
bash wsl/run_matrix.sh expB_unpinned_ttl5000_c64.yaml expB_pinned_ttl5000_c64.yaml
```

## 6. 红线

任何 run 结果里 `success_mismatch > 0` → 立即停止矩阵，保留
results/ 全部日志与 master 二进制现场，走 responsible disclosure。
